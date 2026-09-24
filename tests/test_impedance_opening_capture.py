"""Dialog-owned capture checks avoid duplicate reads without trusting stale pixels."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Union

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

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog, SpecificationDialog
    from impedance.model import Config, Section
    from impedance.review_tracking import ReviewTracking
    from impedance.service import CapturedImage


def _construct(
    env: SimpleNamespace, config: Config, owner: str
) -> Union[ImpedanceDialog, SpecificationDialog]:
    """Use the real main or child constructor with the same production provider."""
    if owner == "main":
        return env.main(config)
    return env.api.dialog.SpecificationDialog(
        None,
        env.captures.snapshot(),
        config.specifications[0],
        preview=env.preview,
        verify_snapshot=env.captures.verify,
        appearance_context=lambda: (env.state.palette, env.state.variant),
        review_tracking=config.review_tracking,
    )


def _show(
    env: SimpleNamespace,
    dialog: Union[ImpedanceDialog, SpecificationDialog],
    owner: str,
) -> None:
    """Let hidden requests settle before showing and painting the native owner."""
    env.api.drain()
    if owner == "main":
        _paint_main(env, dialog)
    else:
        dialog.on_screen = True
        dialog.bindings[env.api.wx.EVT_SHOW, -1](
            SimpleNamespace(
                GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
            )
        )
        env.api.drain()


def _change(env: SimpleNamespace, changed: str) -> None:
    """Change the actual source state and the subsequent native-rendered pixels."""
    from tests.test_impedance_capture_review_restore import _changed_capture

    if changed == "source":
        env.state.snapshot = replace(env.state.snapshot, context_digest="edited board")
    elif changed == "palette":
        env.state.palette = "edited native theme"
    else:
        env.state.variant = "edited output selection"
    env.api.capture = _changed_capture(env.api)


def _history(
    dialog: Union[ImpedanceDialog, SpecificationDialog], owner: str
) -> ReviewTracking:
    """Observe persisted history independently of canvas pixels and pending state."""
    return (
        dialog.session.config.review_tracking
        if owner == "main"
        else dialog.review_tracking
    )


def test_initial_selected_capture_has_four_reads_through_verified_paint(
    save_api: SimpleNamespace,
) -> None:
    """Read once for rows, before render, before install, and after real paint."""
    env = save_api
    dialog = env.main(_config(env))
    env.api.drain()  # Constructor requests run while the dialog is still hidden.
    _paint_main(env, dialog)
    assert dialog.preview_pane.capture_loaded
    assert not dialog.preview_pane.ready
    assert not dialog.session.config.review_tracking.images
    env.api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready
    assert len(dialog.session.config.review_tracking.images) == 1
    assert len(env.state.renders) == 1
    assert len(env.state.source_reads) == 4


@pytest.mark.parametrize("owner", ["main", "child"])
@pytest.mark.parametrize("when", ["during_render", "before_paint"])
@pytest.mark.parametrize("changed", ["source", "palette", "variant"])
def test_current_dialog_never_records_or_saves_capture_from_changed_revision(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    when: str,
    changed: str,
) -> None:
    """Existing approval cannot survive a source change at either display boundary."""
    env = save_api
    config = _approve_child(env, monkeypatch).session.config
    env.captures.close()
    env.reset_counts()
    dialog = _construct(env, config, owner)
    history = _history(dialog, owner)
    stale: list[CapturedImage] = []
    installed: list[CapturedImage] = []
    original_render = env.captures._render
    original_install = dialog.preview_pane.canvas.set_capture

    def render(section: Section) -> CapturedImage:
        capture = original_render(section)
        if not stale:
            stale.append(capture)
            if when == "during_render":
                _change(env, changed)
        return capture

    def install(capture: CapturedImage) -> None:
        installed.append(capture)
        original_install(capture)

    monkeypatch.setattr(env.captures, "_render", render)
    monkeypatch.setattr(dialog.preview_pane.canvas, "set_capture", install)
    _show(env, dialog, owner)
    if when == "before_paint":
        assert dialog.preview_pane.capture_loaded
        _change(env, changed)
        env.api.paint(dialog.preview_pane)
    else:
        assert stale
        assert all(capture is not stale[0] for capture in installed)
    assert not dialog.preview_pane.ready
    assert _history(dialog, owner) == history
    if owner == "main":
        assert not dialog.session.approved
        dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
        assert env.state.saved and not env.state.saved[-1].reviewed_digest
        assert env.state.saved[-1].review_tracking == history
    else:
        assert all(not draft.confirmed for draft in dialog._layer_drafts.values())


def test_new_live_rows_select_current_capture_without_an_intermediate_failure(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary row refresh must not acquire speculative preparation's old marker."""
    env = save_api
    dialog = env.main(_config(env))
    _show(env, dialog, "main")
    env.api.paint(dialog.preview_pane)
    failures: list[str] = []
    report_failure = dialog.preview_pane._report_failure

    def report(message: str) -> None:
        failures.append(message)
        report_failure(message)

    monkeypatch.setattr(dialog.preview_pane, "_report_failure", report)
    _change(env, "source")
    assert dialog._refresh_rows(env.state.snapshot)
    env.api.drain()
    env.api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready
    assert not failures
    assert dialog.session.snapshot == env.state.snapshot
    assert dialog.preview_pane._capture.sha256 == env.api.capture.sha256


@pytest.mark.parametrize("owner", ["main", "child"])
def test_failed_renderer_has_no_view_or_approval_and_automatic_retry_recovers(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, owner: str
) -> None:
    """Only a successful current render followed by actual paint creates a view."""
    env = save_api
    dialog = _construct(env, _config(env), owner)
    original_render = env.captures._render
    attempts = []

    def render(section: Section) -> CapturedImage:
        attempts.append(section)
        if len(attempts) == 1:
            raise ValueError("Temporary native plotter failure")
        return original_render(section)

    monkeypatch.setattr(env.captures, "_render", render)
    _show(env, dialog, owner)
    pane = dialog.preview_pane
    assert "Temporary native plotter failure" in pane.failure
    assert not pane.capture_loaded
    assert not pane.ready
    assert not _history(dialog, owner).images
    assert pane._retry_timer.IsRunning()
    pane.window.bindings[env.api.wx.EVT_TIMER, pane._retry_timer](SimpleNamespace())
    env.api.drain()
    assert env.state.provider_calls[-1][1] is True
    assert pane.capture_loaded and not pane.ready
    assert not _history(dialog, owner).images
    env.api.paint(pane)
    assert pane.ready
    assert len(_history(dialog, owner).images) == 1
    assert not pane.failure


@pytest.mark.parametrize("owner", ["main", "child"])
@pytest.mark.parametrize("when", ["during_render", "before_paint"])
def test_unreadable_appearance_is_rejected_by_real_dialog_and_retry_recovers(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    when: str,
) -> None:
    """The UI owner's appearance check guards both installation and viewed evidence."""
    env = save_api
    dialog = _construct(env, _config(env), owner)
    pane = dialog.preview_pane
    original_appearance = pane._appearance_context
    original_render = env.captures._render
    failed = []

    def appearance() -> object:
        if failed:
            raise OSError("The palette could not be read")
        return original_appearance()

    def render(section: Section) -> CapturedImage:
        capture = original_render(section)
        if when == "during_render":
            failed.append(True)
        return capture

    monkeypatch.setattr(pane, "_appearance_context", appearance)
    monkeypatch.setattr(env.captures, "_render", render)
    _show(env, dialog, owner)
    if when == "before_paint":
        failed.append(True)
        env.api.paint(pane)
    assert "palette could not be read" in pane.failure
    assert not pane.capture_loaded and not pane.ready
    assert not _history(dialog, owner).images
    monkeypatch.setattr(env.captures, "_render", original_render)
    failed.clear()
    pane.window.bindings[env.api.wx.EVT_TIMER, pane._retry_timer](SimpleNamespace())
    env.api.drain()
    env.api.paint(pane)
    assert pane.ready and not pane.failure


@pytest.mark.parametrize("owner", ["main", "child"])
@pytest.mark.parametrize("changed", ["source", "palette", "variant"])
def test_background_raw_capture_cannot_restore_consent_for_changed_revision(
    save_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    changed: str,
) -> None:
    """Preparation without the optional batch seam still has a complete UI boundary."""
    env = save_api
    config = _approve_child(env, monkeypatch).session.config
    env.captures.close()
    dialog = _construct(env, config, owner)
    if owner == "main":
        dialog.prepare_capture = None
    _show(env, dialog, owner)
    env.api.paint(dialog.preview_pane)
    history = _history(dialog, owner)
    selected = dialog.preview_pane.selected_section
    target = next(
        section for section in dialog.preview_pane.sections if section != selected
    )
    mutated = []
    original_render = env.captures._render

    def render(section: Section) -> CapturedImage:
        capture = original_render(section)
        if section == target:
            mutated.append(True)
            _change(env, changed)
        return capture

    monkeypatch.setattr(env.captures, "_render", render)
    for _ in range(3):
        if owner == "main":
            dialog.bindings[env.api.wx.EVT_IDLE, -1](
                SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
            )
        else:
            dialog._capture_prepare_timer.running = False
            dialog.bindings[env.api.wx.EVT_TIMER, dialog._capture_prepare_timer](
                SimpleNamespace()
            )
        if mutated:
            break
    assert mutated
    assert _history(dialog, owner) == history
    if owner == "main":
        assert not dialog.session.approved
        assert dialog.session.capture_digests.get(target.section_id) is None
        dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
        assert env.state.saved and not env.state.saved[-1].reviewed_digest
    else:
        assert target not in dialog._capture_hashes
        assert all(not draft.confirmed for draft in dialog._layer_drafts.values())


@pytest.mark.parametrize("changed", ["source", "palette", "variant"])
def test_save_restoration_without_batch_callback_checks_returned_source_and_appearance(
    save_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """The compatibility fallback cannot approve a recapture changed during Save."""
    env = save_api
    config = _approve_child(env, monkeypatch).session.config
    env.captures.close()
    dialog = env.main(config)
    _show(env, dialog, "main")
    env.api.paint(dialog.preview_pane)
    env.api.finish_preparation(dialog)
    assert dialog.session.approved
    history = dialog.session.config.review_tracking
    dialog.verify_captures = None
    env.captures.images.clear()
    env.captures.image_bytes = 0
    original_render = env.captures._render

    def render(section: Section) -> CapturedImage:
        capture = original_render(section)
        _change(env, changed)
        return capture

    monkeypatch.setattr(env.captures, "_render", render)
    dialog.emit(env.api.wx.EVT_BUTTON, env.api.wx.ID_OK)
    assert env.state.saved and not env.state.saved[-1].reviewed_digest
    assert env.state.saved[-1].review_tracking == history
