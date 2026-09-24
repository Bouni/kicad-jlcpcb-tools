"""Speculative captures must not become foreground work or current consent."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.test_impedance_main_opening import _paint_main
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


def test_show_does_not_repeat_content_column_measurement(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping the already populated workbook does not change its font or values."""
    env = save_api
    dialog = env.main(_config(env, 6))
    fits = []
    monkeypatch.setattr(dialog, "_fit_workbook_columns", lambda: fits.append(True))
    dialog.on_screen = True
    dialog.bindings[env.api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
        )
    )
    env.api.drain()
    assert fits == []


def test_waiting_for_selected_paint_does_not_poll_with_a_native_timer(
    save_api: SimpleNamespace,
) -> None:
    """A loaded image must paint before speculative native rendering starts."""
    env = save_api
    dialog = env.main(_config(env, 6))
    _paint_main(env, dialog)
    assert dialog.preview_pane.capture_loaded
    assert not dialog.preview_pane.ready
    assert not dialog._capture_prepare_timer.IsRunning()
    assert len(env.state.renders) == 1


def test_background_capture_reuses_checked_geometry_without_board_scans(
    save_api: SimpleNamespace,
) -> None:
    """Warming a bound renderer never rereads a board for every image."""
    env = save_api
    dialog = env.main(_config(env, 6))
    _paint_main(env, dialog)
    env.api.paint(dialog.preview_pane)
    history = dialog.session.config.review_tracking
    env.reset_counts()
    env.api.finish_preparation(dialog)
    assert len(env.state.renders) == 5
    assert env.state.source_reads == []
    assert env.state.analyses == []
    assert dialog.session.config.review_tracking == history


def test_successful_draft_warming_does_not_relayout_status(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another prepared image changes neither draft readiness nor visible status."""
    env = save_api
    dialog = env.main(_config(env, 6))
    _paint_main(env, dialog)
    env.api.paint(dialog.preview_pane)
    layouts = []
    monkeypatch.setattr(dialog, "Layout", lambda: layouts.append(True))
    env.api.finish_preparation(dialog)
    assert layouts == []


@pytest.mark.parametrize("cached_widths", [False, True])
def test_first_paint_does_not_rebuild_rows_for_unchanged_widths(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    cached_widths: bool,
) -> None:
    """A no-op calculator check preserves the initial rows and preview request."""
    env = save_api
    config = _config(env, 6)
    if cached_widths:
        from tests.impedance_performance_fixtures import routed_workbook

        workbook = routed_workbook(env.api, env.api.capture.sha256)
        env.state.snapshot, config = workbook.snapshot, workbook.draft
    dialog = env.main(config)
    rebuilds = []
    original = dialog._populate_sections

    def populate(preferred_spec_id: object = None) -> None:
        rebuilds.append(True)
        original(preferred_spec_id)

    monkeypatch.setattr(dialog, "_populate_sections", populate)
    _paint_main(env, dialog)
    assert rebuilds == []


@pytest.mark.parametrize("changed", ["source", "palette", "variant"])
def test_save_checks_revision_before_waiting_for_old_capture_restoration(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """Obsolete historical preparation must not block saving current draft intent."""
    env = save_api
    config = _approve_child(env, monkeypatch, 6).session.config
    env.captures.close()
    dialog = env.main(config)
    assert dialog._capture_restore_pending
    assert dialog.FindWindow(env.api.wx.ID_OK).enabled
    if changed == "source":
        env.state.snapshot = replace(env.state.snapshot, context_digest="new board")
    elif changed == "palette":
        env.state.palette = "new palette"
    else:
        env.state.variant = "new output"
    env.reset_counts()
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
    assert dialog.modal_result == env.api.wx.ID_OK
    assert env.state.saved[-1].specifications == config.specifications
    assert not env.state.saved[-1].reviewed_digest
    assert env.state.saved[-1].review_tracking == config.review_tracking
    assert env.state.renders == []
