"""Require explicit spacing-concern approval independently of manufacturing rows."""

from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from .test_impedance_spacing_dialog import (
    _config,
    _gap_board,
    _no_catalog_workers,  # noqa: F401 -- fixture prevents unrelated network work
    constructor_api,  # noqa: F401 -- required by draft_api
    draft_api as _draft_api,
)

draft_api = _draft_api


@pytest.fixture(autouse=True)
def _large_viewer_harness(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive real large-canvas paints and explicit modal consent in UI workflows."""
    from .test_impedance_netclass_dialog import _install_spacing_viewer_harness

    _install_spacing_viewer_harness(draft_api, monkeypatch)


STAMP = "2026-10-24T12:00:00.000000Z"


def _session(api: SimpleNamespace) -> Any:
    """Create an actual measured wrong-gap pair with no prior diagnostic consent."""
    session = api.dialog.ReviewSession(_config(api), _gap_board(api, 250_000))
    session.refresh()
    assert session.spacing_entries
    return session


def _separate_concern_board(api: SimpleNamespace, *, count: int = 2) -> Any:
    """Keep short, physically disconnected departures on the same net pair."""
    board = _gap_board(api, 250_000)
    return replace(
        board,
        traces=(
            *board.traces,
            *(
                replace(
                    trace,
                    trace_id=f"separate-{index}-{trace.trace_id}",
                    points=tuple(
                        (x + index * 60_000_000, y + index * 5_000_000)
                        for x, y in trace.points
                    ),
                )
                for index in range(1, count)
                for trace in board.traces[:2]
            ),
        ),
    )


def _show(api: SimpleNamespace, dialog: Any, index: int) -> None:
    """Deliver loading and the production pane's successful paint callback."""
    dialog.preview_pane.select(index)
    api.drain()
    api.paint(dialog.preview_pane)


def _editor(api: SimpleNamespace, *, history: Any = None) -> Any:
    """Open one active differential layer through the real specification editor."""
    options = {} if history is None else {"review_tracking": history}
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 250_000),
        _config(api).specifications[0],
        preview=api.preview,
        **options,
    )
    editor.on_screen = True
    api.drain()
    api.paint(editor.preview_pane)
    assert editor.preview_pane.spacing_entries
    return editor


def test_editor_list_selection_names_the_spacing_exception_action(
    draft_api: SimpleNamespace,
) -> None:
    """The actual image-list handler distinguishes exception consent from trace review."""
    api = draft_api
    editor = _editor(api)
    pane = editor.preview_pane
    trace, spacing = pane.sections[0], pane.spacing_entries[0]
    for entry, label in (
        (trace, "Approve image"),
        (spacing, "Approve spacing exception"),
        (trace, "Approve image"),
        (spacing, "Approve spacing exception"),
    ):
        pane.checklist.Select(pane.entries.index(entry))
        pane.checklist.emit(api.wx.EVT_LIST_ITEM_SELECTED)
        api.drain()
        api.paint(pane)
        assert pane.selected_entry is entry
        assert editor.approve_layer.GetLabel() == label
        assert not editor.review_tracking.spacing_images
    assert not editor._layer_drafts["F.Cu"].confirmed


def test_workbook_approval_cannot_bulk_approve_spacing_images(
    draft_api: SimpleNamespace,
) -> None:
    """Viewing diagnostics and checking their parent rows grant no consent."""
    api = draft_api
    session = _session(api)
    entries = session.spacing_entries
    for entry in entries:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
    assert session.pending_spacing_reviews == entries
    assert not session.approved
    assert not session.approve_if_layer_reviews_complete()
    with pytest.raises(ValueError, match="spacing image"):
        session.approve()
    assert all(
        not record.approved_digest
        for record in session.config.review_tracking.spacing_images
    )


def test_unchecking_parent_rows_cannot_remove_spacing_review(
    draft_api: SimpleNamespace,
) -> None:
    """The differential pair's active layer is assessed independently of row toggles."""
    session = _session(draft_api)
    before = session.spacing_entries
    session.set_included(())
    assert session.spacing_entries == before
    assert session.pending_spacing_reviews == before


def test_session_requires_current_view_before_individual_approval(
    draft_api: SimpleNamespace,
) -> None:
    """The explicit approval API rejects absent or different displayed pixels."""
    api = draft_api
    session = _session(api)
    entry = session.spacing_entries[0]
    with pytest.raises(ValueError, match="Display"):
        session.approve_spacing_image(entry, api.capture.sha256)
    session.record_spacing_view(entry, api.capture.sha256, STAMP)
    with pytest.raises(ValueError, match="Display"):
        session.approve_spacing_image(entry, "b" * 64)
    session.approve_spacing_image(entry, api.capture.sha256)
    assert session.spacing_review_current(entry)
    previous = session.config.review_tracking.spacing_images
    session.approve_spacing_image(entry, api.capture.sha256)
    assert session.config.review_tracking.spacing_images == previous


def test_save_reopen_requires_actual_spacing_png_verification_without_new_events(
    draft_api: SimpleNamespace,
) -> None:
    """Restoring manufacturing approval never authorizes unverified diagnostics."""
    api = draft_api
    session = _session(api)
    for entry in session.spacing_entries:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    saved = api.model.Config.from_dict(session.save_result().to_dict())
    before = saved.review_tracking
    reopened = api.dialog.ReviewSession(saved, session.snapshot)
    assert reopened.workbook_review_current
    assert not reopened.approved
    assert reopened.pending_spacing_reviews
    for entry in reopened.spacing_entries:
        reopened.spacing_capture_digests[entry.view_id] = (
            api.dialog.tracking.spacing_capture_fingerprint(
                entry.input_digest, api.capture.sha256
            )
        )
    assert reopened.approved
    assert reopened.save_result().review_tracking == before


@pytest.mark.parametrize("change", ["pixels", "target", "geometry"])
def test_changed_spacing_evidence_revokes_approval(
    draft_api: SimpleNamespace,
    change: str,
) -> None:
    """An unchanged timestamp cannot authorize changed diagnostic inputs or PNGs."""
    api = draft_api
    session = _session(api)
    for entry in session.spacing_entries:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    if change == "pixels":
        session.record_spacing_view(session.spacing_entries[0], "b" * 64, STAMP)
    elif change == "target":
        spec = session.config.specifications[0]
        settings = replace(spec.layer_settings[0], spacing_nm=190_000)
        session.replace_specifications((replace(spec, layer_settings=(settings,)),))
        session.refresh()
    else:
        session.refresh(_gap_board(api, 260_000))
    assert not session.approved
    assert session.pending_spacing_reviews
    assert not session.save_result().reviewed_digest
    assert session.config.review_tracking.spacing_images


def test_editor_separate_closeup_needs_explicit_visible_approval(
    draft_api: SimpleNamespace,
) -> None:
    """Ordinary image approval advances to a diagnostic without approving it."""
    api = draft_api
    editor = _editor(api)
    ordinary = editor.preview_pane.sections
    for section in ordinary:
        _show(api, editor, editor.preview_pane.entries.index(section))
        editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert not editor._layer_drafts["F.Cu"].confirmed
    selected = editor.preview_pane.selected_entry
    assert selected in editor.preview_pane.spacing_entries
    assert editor.approve_layer.GetLabel() == "Approve spacing exception"
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert not editor.review_tracking.spacing_images
    api.drain()
    api.paint(editor.preview_pane)
    assert not editor.review_tracking.spacing_images
    assert editor.approve_layer.enabled
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor.review_tracking.spacing_images[0].approved_digest
    assert editor._layer_drafts["F.Cu"].confirmed
    assert editor.preview_pane.sections == ordinary
    assert editor.preview_pane._capture.data == api.capture.data
    assert editor.preview_pane.caption.GetLabel()
    assert editor.preview_pane.reviewed_capture_hashes() == tuple(
        (section, api.capture.sha256) for section in ordinary
    )


@pytest.mark.parametrize("state", ["queued", "hidden", "failed"])
def test_editor_cannot_approve_spacing_without_successful_visible_paint(
    draft_api: SimpleNamespace,
    state: str,
) -> None:
    """Each diagnostic's actual display remains mandatory after restoring history."""
    api = draft_api
    editor = _editor(api)
    index = editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0])
    editor.preview_pane.select(index, refresh=True)
    if state == "failed":

        def unavailable(*_args: object, **_kwargs: object) -> Any:
            raise ValueError("Spacing capture failed")

        editor.preview_pane._preview = unavailable
    api.drain()
    if state == "hidden":
        api.paint(editor.preview_pane)
        editor.on_screen = False
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert all(
        not record.approved_digest for record in editor.review_tracking.spacing_images
    )
    assert not editor._layer_drafts["F.Cu"].confirmed


def test_editor_cancel_and_apply_keep_diagnostic_events_transactional(
    draft_api: SimpleNamespace,
) -> None:
    """A staged close-up approval survives Apply and cannot mutate caller history."""
    api = draft_api
    history = api.dialog.tracking.ReviewTracking()
    editor = _editor(api, history=history)
    _show(
        api,
        editor,
        editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0]),
    )
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor.review_tracking.spacing_images[0].approved_digest
    assert history == api.dialog.tracking.ReviewTracking()
    editor.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert editor.specification is not None
    assert editor.review_tracking.spacing_images[0].approved_digest
    restored = api.model.Config.from_dict(
        api.model.Config(review_tracking=editor.review_tracking).to_dict()
    )
    reopened = _editor(api, history=restored.review_tracking)
    assert (
        reopened.review_tracking.spacing_images == editor.review_tracking.spacing_images
    )
    # The initially visible diagnostic has already verified its current PNG.
    assert not reopened._pending_spacing_entries("F.Cu")
    for entry in reopened.preview_pane.spacing_entries:
        reopened._prepared_preview(entry)
    assert reopened.spacing_capture_digests
    assert not reopened._pending_spacing_entries("F.Cu")
    assert (
        reopened.review_tracking.spacing_images == editor.review_tracking.spacing_images
    )


def test_policy6_to_percentage_review_requires_new_consent_and_preserves_saved_history(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Preparation/Cancel cannot consent or write; explicit policy-7 approval retains both histories."""
    from impedance.model import Config as StoredConfig

    from .test_impedance_dialog_preview import _png
    from .test_impedance_netclass_dialog import _drive_spacing_viewer, apply_layer
    from .test_impedance_spacing_tracking import _repository

    api = draft_api
    policies = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    service = importlib.import_module(api.dialog.__package__ + ".service")
    tracking = api.dialog.tracking
    capture = service.CapturedImage.from_bytes(
        _png(size=(policies.SPACING_IMAGE_WIDTH, policies.SPACING_IMAGE_HEIGHT))
    )
    monkeypatch.setattr(api, "capture", capture)
    monkeypatch.setattr(api, "preview", lambda *_args, **_kwargs: capture)
    with monkeypatch.context() as old_renderer:
        old_renderer.setattr(policies, "SPACING_RENDER_POLICY_REVISION", 6)
        old_renderer.setattr(tracking, "utc_now", lambda: STAMP)
        editor = _editor(api)
        old_entries = editor.preview_pane.spacing_entries
        apply_layer(api, editor)
        assert editor._layer_drafts["F.Cu"].confirmed
        session = api.dialog.ReviewSession(
            replace(_config(api), review_tracking=editor.review_tracking),
            _gap_board(api, 250_000),
        )
        session.refresh()
        session.capture_digests.update(editor.capture_digests)
        session.spacing_capture_digests.update(editor.spacing_capture_digests)
        session.approve()
        saved = session.save_result()
    historical = saved.review_tracking
    assert historical.images and historical.layers and historical.spacing_images
    assert all(record.approved_at_utc == STAMP for record in historical.spacing_images)
    repository = _repository(tmp_path)
    revision = [repository.save(StoredConfig.from_dict(saved.to_dict()), 0)]
    stored_before = repository.database.load_config(repository.board_id)
    database_before = repository.database.path.read_bytes()
    writes: list[Any] = []

    def persist(config: Any) -> None:
        """Write the real Save event's candidate through the board-scoped SQLite repository."""
        writes.append(config)
        revision[0] = repository.save(
            StoredConfig.from_dict(config.to_dict()), revision[0]
        )

    def reopen() -> Any:
        """Reload saved bytes and exercise the actual owner, capture, and paint handlers."""
        config, current_revision = repository.load()
        assert current_revision == revision[0]
        dialog = api.dialog.ImpedanceDialog(
            None,
            api.model.Config.from_dict(config.to_dict()),
            session.snapshot,
            api.preview,
            lambda: session.snapshot,
            save_config=persist,
        )
        dialog.on_screen = True
        dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
        api.drain()
        api.paint(dialog.preview_pane)
        api.finish_preparation(dialog)
        return dialog

    current = reopen()
    entries = current.preview_pane.spacing_entries
    assert current.session.pending_spacing_reviews == entries and entries
    assert policies.SPACING_RENDER_POLICY_REVISION == 7
    assert {entry.view_id for entry in entries}.isdisjoint(
        entry.view_id for entry in old_entries
    )
    assert current.session.config.review_tracking == historical
    assert all(
        current.session.spacing_capture_digests[entry.view_id]
        == tracking.spacing_capture_fingerprint(entry.input_digest, api.capture.sha256)
        for entry in entries
    )
    assert {
        section.section_id for section in current.preview_pane.sections
    } <= current._approved_main_image_ids()
    assert not current.session.approved
    with pytest.raises(api.model.ValidationError, match="individual approval"):
        service.prepare(current.session.config, session.snapshot)

    def cancel_readable_view(viewer: Any) -> int:
        """Cancel only after the real enlarged canvas records readable current pixels."""
        result = _drive_spacing_viewer(api, viewer, approve=False)
        assert viewer._composition is not None
        assert (
            viewer._composition.schematic_rect in viewer._composition.required_regions
        )
        assert viewer.canvas._recorded and not viewer.approved
        return result

    with monkeypatch.context() as cancelled_view:
        cancelled_view.setattr(api.wx.Dialog, "ShowModal", cancel_readable_view)
        current.spacing_approve_button.emit(api.wx.EVT_BUTTON)
    new_view = tracking.find_spacing_image(
        current.session.config.review_tracking,
        entries[0].spec_id,
        entries[0].layer,
        entries[0].view_id,
    )
    assert (
        new_view is not None and new_view.viewed_digest and not new_view.approved_digest
    )
    assert current.session.pending_spacing_reviews == entries
    assert all(
        record in current.session.config.review_tracking.spacing_images
        for record in historical.spacing_images
    )
    assert current.session.config.review_tracking.images == historical.images
    assert current.session.config.review_tracking.layers == historical.layers
    current.emit(api.wx.EVT_BUTTON, api.wx.ID_CANCEL)
    assert current._closing and writes == []
    assert repository.database.load_config(repository.board_id) == stored_before
    assert repository.database.path.read_bytes() == database_before
    assert repository.load()[1] == 1

    current = reopen()
    assert current.session.config.review_tracking == historical
    assert (
        current.session.pending_spacing_reviews == current.preview_pane.spacing_entries
    )
    fresh_stamp = "2026-10-27T12:00:00.000000Z"
    monkeypatch.setattr(tracking, "utc_now", lambda: fresh_stamp)
    for entry in current.preview_pane.spacing_entries:
        _show(api, current, current.preview_pane.entries.index(entry))
        current.spacing_approve_button.emit(api.wx.EVT_BUTTON)
        assert current.session.spacing_review_current(entry)
    assert current.session.approved
    assert current.session.config.review_tracking.images == historical.images
    assert current.session.config.review_tracking.layers == historical.layers
    current.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert current.modal_result == api.wx.ID_OK and len(writes) == 1
    persisted_history = current.session.config.review_tracking
    assert len(persisted_history.spacing_images) == len(old_entries) + len(entries)
    assert all(
        record in persisted_history.spacing_images
        for record in historical.spacing_images
    )
    assert {
        record.approved_at_utc
        for record in persisted_history.spacing_images
        if record.view_id in {entry.view_id for entry in entries}
    } == {fresh_stamp}

    def unexpected_event() -> str:
        raise AssertionError(
            "Reopening current captures must not manufacture review events"
        )

    monkeypatch.setattr(tracking, "utc_now", unexpected_event)
    final = reopen()
    assert revision == [2] and final.session.approved
    assert final.session.config.review_tracking == persisted_history
    assert final.session.config.review_tracking.images == historical.images
    assert final.session.config.review_tracking.layers == historical.layers
    assert service.prepare(final.session.config, session.snapshot) is not None


def test_two_spacing_images_require_two_individual_approvals(
    draft_api: SimpleNamespace,
) -> None:
    """Approving one physical concern cannot approve a disconnected departure."""
    api = draft_api
    board = _separate_concern_board(api)
    session = api.dialog.ReviewSession(_config(api), board)
    session.refresh()
    assert len(session.spacing_entries) == 2
    first, second = session.spacing_entries[:2]
    session.record_spacing_view(first, api.capture.sha256, STAMP)
    session.approve_spacing_image(first, api.capture.sha256)
    assert first not in session.pending_spacing_reviews
    assert second in session.pending_spacing_reviews
    assert len(session.config.review_tracking.spacing_images) == 1
    with pytest.raises(ValueError, match="spacing image"):
        session.approve()
    for entry in session.spacing_entries[1:]:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    assert session.approved


def test_disabled_draft_still_exposes_spacing_images_for_explicit_review(
    draft_api: SimpleNamespace,
) -> None:
    """Draft activation state must not hide concerns from its review UI."""
    api = draft_api
    session = api.dialog.ReviewSession(
        replace(_config(api), enabled=False), _gap_board(api, 250_000)
    )
    session.refresh()
    assert session.spacing_entries
    with pytest.raises(ValueError, match="spacing image"):
        session.approve()


def test_main_workbook_button_navigates_to_pending_spacing_without_approving(
    draft_api: SimpleNamespace,
) -> None:
    """The real main-window approval handlers keep bulk and individual consent apart."""
    api = draft_api
    board = _gap_board(api, 250_000)
    dialog = api.dialog.ImpedanceDialog(
        None, _config(api), board, api.preview, lambda: board
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    ordinary = dialog.preview_pane.sections
    entries = dialog.preview_pane.spacing_entries
    assert entries
    _show(api, dialog, dialog.preview_pane.entries.index(ordinary[0]))
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.preview_pane.selected_entry in entries
    assert not dialog.session.approved
    assert dialog.session.config.review_tracking.spacing_images == ()
    assert "spacing" in dialog.review_status.GetLabel()
    dialog.spacing_approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.config.review_tracking.spacing_images == ()
    api.drain()
    api.paint(dialog.preview_pane)
    assert not dialog.session.config.review_tracking.spacing_images
    assert dialog.spacing_approve_button.enabled
    dialog.spacing_approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.config.review_tracking.spacing_images[0].approved_digest
    assert not dialog.session.approved
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved
    assert dialog.preview_pane.sections == ordinary
    assert dialog.session.config.included_section_ids == tuple(
        section.section_id for section in ordinary
    )


def test_main_appearance_change_revokes_diagnostic_pixel_evidence(
    draft_api: SimpleNamespace,
) -> None:
    """Old diagnostic ledgers cannot authorize a changed visual capture context."""
    api = draft_api
    session = _session(api)
    for entry in session.spacing_entries:
        session.record_spacing_view(entry, api.capture.sha256, STAMP)
        session.approve_spacing_image(entry, api.capture.sha256)
    session.approve()
    palette = ["before"]
    dialog = api.dialog.ImpedanceDialog(
        None,
        session.config,
        session.snapshot,
        api.preview,
        lambda: session.snapshot,
        appearance_context=lambda: palette[0],
    )
    dialog.session.spacing_capture_digests.update(session.spacing_capture_digests)
    assert dialog.session.approved
    palette[0] = "after"
    with pytest.raises(ValueError, match="appearance changed"):
        dialog.preview_pane.ensure_appearance_current()
    assert dialog.session.pending_spacing_reviews
    assert dialog.session.spacing_capture_digests == {}


def test_issue_identity_mismatch_cannot_restore_spacing_consent(
    draft_api: SimpleNamespace,
) -> None:
    """A copied view/input digest cannot authorize a different diagnostic issue."""
    api = draft_api
    session = _session(api)
    entry = session.spacing_entries[0]
    session.record_spacing_view(entry, api.capture.sha256, STAMP)
    session.approve_spacing_image(entry, api.capture.sha256)
    record = session.config.review_tracking.spacing_images[0]
    session.config = replace(
        session.config,
        review_tracking=replace(
            session.config.review_tracking,
            spacing_images=(replace(record, issue_id="b" * 64),),
        ),
    )
    assert not session.spacing_review_current(entry)
    with pytest.raises(ValueError, match="Display"):
        session.approve_spacing_image(entry, api.capture.sha256)
    session.record_spacing_view(entry, api.capture.sha256, STAMP)
    assert not session.spacing_review_current(entry)
    assert not session.config.review_tracking.spacing_images[0].approved_digest


def test_capture_failure_remains_pending_after_background_recapture(
    draft_api: SimpleNamespace,
) -> None:
    """A successful render cannot substitute for recovery from a failed visible paint."""
    api = draft_api
    editor = _editor(api)
    index = editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0])
    _show(api, editor, index)
    entry = editor.preview_pane.selected_entry
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._spacing_record_current(entry)
    _show(api, editor, index)
    editor.preview_pane._report_failure("Native spacing paint failed")
    assert not editor._spacing_record_current(entry)
    editor._prepared_preview(entry)
    assert not editor._spacing_record_current(entry)
    assert editor._pending_spacing_entries("F.Cu")
    _show(api, editor, index)
    assert not editor._spacing_record_current(entry)
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._spacing_record_current(entry)


def test_spacing_image_limit_is_visible_and_blocks_layer_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded diagnostic workload cannot silently disappear from approval gates."""
    import importlib

    api = draft_api
    entry_module = importlib.import_module(api.dialog.__package__ + ".spacing_review")

    def too_many(*_args: object, **_kwargs: object) -> Any:
        raise ValueError("More than 256 spacing review images are required")

    monkeypatch.setattr(entry_module, "spacing_review_entries", too_many)
    editor = api.dialog.SpecificationDialog(
        None,
        _gap_board(api, 250_000),
        _config(api).specifications[0],
        preview=api.preview,
    )
    editor.on_screen = True
    api.drain()
    api.paint(editor.preview_pane)
    assert "256 spacing review images" in editor.spacing_comparison.GetLabel()
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert "256 spacing review images" in editor.validation_message.GetLabel()
    assert not editor._layer_drafts["F.Cu"].confirmed


def test_long_spacing_floor_is_reviewed_in_large_modal_before_individual_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cropped physical detail cannot replace legible full-composition consent."""
    import importlib

    from .test_impedance_dialog_preview import _png
    from .test_impedance_netclass_dialog import _drive_spacing_viewer

    api = draft_api
    board = _gap_board(api, 250_000)
    board = replace(
        board,
        traces=tuple(
            replace(trace, points=(trace.points[0], (40_000_000, trace.points[-1][1])))
            for trace in board.traces
        ),
    )
    service = importlib.import_module(api.dialog.__package__ + ".service")
    entries = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    capture = service.CapturedImage.from_bytes(
        _png(size=(entries.SPACING_IMAGE_WIDTH, entries.SPACING_IMAGE_HEIGHT))
    )
    editor = api.dialog.SpecificationDialog(
        None,
        board,
        _config(api).specifications[0],
        preview=lambda *_args, **_kwargs: capture,
    )
    editor.on_screen = True
    _show(
        api,
        editor,
        editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0]),
    )
    entry = editor.preview_pane.selected_entry
    detail_width, detail_height = entry.composition.detail_rect[2:]
    native_feature = (
        min(trace.width_nm for trace in entry.traces)
        * detail_width
        / entry.viewport_nm[2]
    )
    assert native_feature == pytest.approx(24)
    assert editor.preview_pane.canvas._scaled_view == entry.composition.detail_rect
    assert editor.preview_pane.canvas._presentation_crop is not None
    assert not editor.review_tracking.spacing_images
    inspected: list[object] = []

    def show_modal(viewer: Any) -> int:
        """Measure the actual scaled bitmap from its explicit native-like paint."""
        result = _drive_spacing_viewer(api, viewer)
        assert native_feature * viewer.canvas._painted_scale >= 8
        inspected.append(viewer)
        return result

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", show_modal)
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert len(inspected) == 1, editor.validation_message.GetLabel()
    assert editor.review_tracking.spacing_images[0].approved_digest
    assert len(editor.review_tracking.spacing_images) == 1
    assert len(editor.preview_pane.spacing_entries) == 1
    assert editor._pending_spacing_entries("F.Cu") == ()


def test_cancelled_large_spacing_view_keeps_the_image_pending(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The modal's Cancel leaves a valid view event without diagnostic consent."""
    from .test_impedance_netclass_dialog import _drive_spacing_viewer

    api = draft_api
    editor = _editor(api)
    _show(
        api,
        editor,
        editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0]),
    )
    entry = editor.preview_pane.selected_entry
    monkeypatch.setattr(
        api.wx.Dialog,
        "ShowModal",
        lambda viewer: _drive_spacing_viewer(api, viewer, approve=False),
    )
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor.review_tracking.spacing_images[0].viewed_digest
    assert not editor.review_tracking.spacing_images[0].approved_digest
    assert not editor._spacing_record_current(entry)
    assert entry in editor._pending_spacing_entries("F.Cu")


@pytest.mark.parametrize("legacy_policy", [None, 1, 6])
def test_saved_manufacturing_approval_cannot_label_pending_readable_reviews_approved(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    legacy_policy: Optional[int],
) -> None:
    """A newly required readable-image review stays visible in the class status."""
    import importlib

    api = draft_api
    policies = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    current_policy = policies.SPACING_RENDER_POLICY_REVISION
    if legacy_policy:
        monkeypatch.setattr(policies, "SPACING_RENDER_POLICY_REVISION", legacy_policy)
    editor = _editor(api)
    for index in range(len(editor.preview_pane.entries)):
        _show(api, editor, index)
        editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._layer_drafts["F.Cu"].confirmed
    session = api.dialog.ReviewSession(
        replace(_config(api), review_tracking=editor.review_tracking),
        _gap_board(api, 250_000),
    )
    session.refresh()
    session.capture_digests.update(editor.capture_digests)
    session.spacing_capture_digests.update(editor.spacing_capture_digests)
    session.approve()
    config = session.config
    if legacy_policy:
        assert config.review_tracking.spacing_images
        monkeypatch.setattr(policies, "SPACING_RENDER_POLICY_REVISION", current_policy)
    else:
        config = replace(
            config, review_tracking=replace(config.review_tracking, spacing_images=())
        )
    ordinary_history = config.review_tracking.layers
    dialog = api.dialog.ImpedanceDialog(
        None, config, session.snapshot, api.preview, lambda: session.snapshot
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    api.finish_preparation(dialog)
    dialog._populate_specifications()
    assert dialog.session.pending_spacing_reviews
    assert dialog._review_row_labels == [api.dialog.SPEC_REVIEW_NEEDS_APPROVAL]
    assert "spacing images need approval" in dialog.review_status.GetLabel()
    assert dialog.session.config.review_tracking.layers == ordinary_history
    ordinary_ids = {section.section_id for section in dialog.preview_pane.sections}
    assert ordinary_ids <= dialog._approved_main_image_ids()

    reopened = _editor(api, history=config.review_tracking)
    api.finish_preparation(reopened)
    assert reopened._pending_spacing_entries("F.Cu")
    assert not reopened._layer_drafts["F.Cu"].confirmed
    assert "F.Cu — needs review" in reopened.layer.items
    assert "Previous approval" in reopened.approval_hint.GetLabel()
    assert {
        section.section_id for section in reopened.preview_pane.sections
    } <= reopened._approved_image_ids()
    assert reopened.review_tracking.layers == ordinary_history


def test_current_large_paint_failure_revokes_prior_diagnostic_evidence(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed large native paint cannot reuse earlier consent in this session."""
    from .test_impedance_netclass_dialog import _drive_spacing_viewer

    api = draft_api
    editor = _editor(api)
    index = editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0])
    _show(api, editor, index)
    entry = editor.preview_pane.selected_entry
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._spacing_record_current(entry)
    history = editor.review_tracking
    _show(api, editor, index)

    def failed_modal(viewer: Any) -> int:
        """Fail the actual bitmap scaling paint, then cancel the failed viewer."""
        viewer.canvas._image.valid = False
        return _drive_spacing_viewer(api, viewer, approve=False)

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", failed_modal)
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor.review_tracking == history
    assert not editor._spacing_record_current(entry)
    assert entry in editor._capture_display_failed
    assert entry in editor._pending_spacing_entries("F.Cu")
    assert not editor._layer_drafts["F.Cu"].confirmed


def test_late_large_view_failure_cannot_fault_the_new_selected_image(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued failure from an obsolete modal cannot taint another capture."""
    api = draft_api
    editor = _editor(api)
    _show(
        api,
        editor,
        editor.preview_pane.entries.index(editor.preview_pane.spacing_entries[0]),
    )
    history = editor.review_tracking

    def obsolete_modal(viewer: Any) -> int:
        """Deliver a real deferred failure only after native selection changed."""
        viewer.on_screen = True
        api.wx.CallAfter(
            viewer.canvas._paint_failed,
            viewer.canvas._generation,
            "Old image paint failed",
        )
        editor.preview_pane.select(
            editor.preview_pane.entries.index(editor.preview_pane.sections[0])
        )
        api.drain()
        viewer.EndModal(api.wx.ID_CANCEL)
        return api.wx.ID_CANCEL

    monkeypatch.setattr(api.wx.Dialog, "ShowModal", obsolete_modal)
    editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert not editor.preview_pane.failure
    assert not editor._capture_display_failed
    assert editor.review_tracking == history


def _approved_separate_spacing_concerns(
    api: SimpleNamespace, *, count: int = 2
) -> tuple[Any, Any]:
    """Persist actual individual modal approvals for distinct concerns."""
    board = _separate_concern_board(api, count=count)
    config = _config(api)
    editor = api.dialog.SpecificationDialog(
        None, board, config.specifications[0], preview=api.preview
    )
    editor.on_screen = True
    assert len(editor.preview_pane.spacing_entries) == count
    for index in range(len(editor.preview_pane.entries)):
        _show(api, editor, index)
        editor.approve_layer.emit(api.wx.EVT_BUTTON)
    assert editor._layer_drafts["F.Cu"].confirmed
    session = api.dialog.ReviewSession(
        replace(config, review_tracking=editor.review_tracking), board
    )
    session.refresh()
    session.capture_digests.update(editor.capture_digests)
    session.spacing_capture_digests.update(editor.spacing_capture_digests)
    session.approve()
    return api.model.Config.from_dict(session.save_result().to_dict()), board


def _spacing_evidence(
    api: SimpleNamespace,
    hashes: dict[Any, Any],
    *,
    current: bool = True,
) -> Any:
    """Use the production frozen evidence contract at the mocked native boundary."""
    import importlib

    service = importlib.import_module(api.dialog.__package__ + ".service")
    return service.SpacingCaptureEvidence(current=current, hashes=hashes)


def test_save_verifies_distinct_concerns_once_without_rewriting_review_history(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Save batches every required PNG and refreshes diagnostic labels only once."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    saved: list[Any] = []
    calls: list[tuple[Any, Any]] = []

    def verify(snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Return independently verified bytes without reading each image again."""
        calls.append((snapshot, required))
        return _spacing_evidence(api, dict.fromkeys(required, api.capture.sha256))

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        save_config=saved.append,
        verify_spacing_captures=verify,
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    api.finish_preparation(dialog)
    assert dialog.session.approved
    entries = dialog.session.spacing_entries
    history = dialog.session.config.review_tracking
    calls.clear()
    restored: list[dict[Any, str]] = []
    original_restore = dialog.preview_pane.restore_spacing_viewed

    def restore(matches: Any, hashes: dict[Any, str]) -> None:
        """Observe the actual bulk restore without changing its evidence rules."""
        restored.append(dict(hashes))
        original_restore(matches, hashes)

    def preview(entry: Any, **kwargs: Any) -> Any:
        """Reject diagnostic reads outside the source-checked batch."""
        assert entry not in entries, "Save reread a diagnostic outside its batch"
        return api.preview(entry, **kwargs)

    dialog.preview_callback = preview
    monkeypatch.setattr(dialog.preview_pane, "restore_spacing_viewed", restore)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert calls == [(board, entries)]
    assert restored == [dict.fromkeys(entries, api.capture.sha256)]
    assert len(saved) == 1 and saved[0].reviewed_digest
    assert saved[0].review_tracking == history
    assert (
        saved[0].to_dict()["review_tracking"]["spacing_images"]
        == config.to_dict()["review_tracking"]["spacing_images"]
    )


@pytest.mark.parametrize(
    "failure",
    [
        "changed",
        "missing",
        "partial",
        "wrong_issue",
        "wrong_input",
        "source",
        "palette",
    ],
)
def test_batched_spacing_verification_cannot_reuse_invalid_consent(
    draft_api: SimpleNamespace,
    failure: str,
) -> None:
    """Actual Save retains history while invalid batch evidence revokes approval."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    saved: list[Any] = []
    calls: list[tuple[Any, ...]] = []
    active_failure = [False]

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Model valid-source partial pixels separately from invalid source state."""
        calls.append(required)
        hashes = dict.fromkeys(required, api.capture.sha256)
        if not active_failure[0]:
            return _spacing_evidence(api, hashes)
        if failure == "changed":
            hashes[required[0]] = "b" * 64
        elif failure == "missing":
            hashes[required[0]] = None
        elif failure == "partial":
            hashes.pop(required[0])
        elif failure == "palette":
            raise ValueError("The native appearance could not be verified")
        elif failure == "source":
            return _spacing_evidence(api, hashes, current=False)
        return _spacing_evidence(api, hashes)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        save_config=saved.append,
        verify_spacing_captures=verify,
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    api.finish_preparation(dialog)
    entries = dialog.session.spacing_entries
    expected = entries
    if failure in {"wrong_issue", "wrong_input"}:
        records = dialog.session.config.review_tracking.spacing_images
        changed = replace(
            records[0],
            **{"issue_id" if failure == "wrong_issue" else "input_digest": "b" * 64},
        )
        dialog.session.config = replace(
            dialog.session.config,
            review_tracking=replace(
                dialog.session.config.review_tracking,
                spacing_images=(changed, *records[1:]),
            ),
        )
        expected = tuple(entry for entry in entries if entry.view_id != changed.view_id)
    history = dialog.session.config.review_tracking
    calls.clear()
    active_failure[0] = True
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert calls == [expected]
    assert not dialog.session.approved
    assert not dialog.session.workbook_review_current
    assert dialog.session.config.review_tracking == history
    assert not saved or not saved[0].reviewed_digest
    if failure in {"source", "palette"}:
        assert all(
            dialog.session.spacing_capture_digests.get(entry.view_id) is None
            for entry in entries
        )
    if failure == "palette":
        assert not saved and not dialog._closing
    if failure in {"missing", "partial"}:
        assert not saved and not dialog._closing
        assert all(
            dialog.session.spacing_capture_digests.get(entry.view_id) is None
            for entry in entries
        )


def test_batch_reverification_cannot_restore_failed_visible_spacing_review(
    draft_api: SimpleNamespace,
) -> None:
    """Correct cached PNGs cannot clear a current failed-paint ledger or grant consent."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    dialog = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    dialog.on_screen = True
    _show(
        api,
        dialog,
        dialog.preview_pane.entries.index(dialog.preview_pane.spacing_entries[0]),
    )
    entry = dialog.session.spacing_entries[0]
    history = dialog.session.config.review_tracking
    dialog.preview_pane._report_failure("The current native spacing paint failed")
    calls: list[tuple[Any, ...]] = []

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Recheck bytes without inventing a successful native display event."""
        calls.append(required)
        return _spacing_evidence(api, dict.fromkeys(required, api.capture.sha256))

    dialog.verify_spacing_captures = verify
    dialog._verify_spacing_capture_review()
    assert calls == [dialog.session.spacing_entries]
    assert entry in dialog.session.pending_spacing_reviews
    assert entry.view_id in dialog.session.spacing_display_failures
    assert not dialog.session.workbook_review_current
    assert dialog.session.config.review_tracking == history


def test_spacing_save_without_batch_keeps_per_image_source_verification(
    draft_api: SimpleNamespace,
) -> None:
    """Standalone callers retain the original actual-PNG and source checks."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    saved: list[Any] = []
    checks: list[Any] = []

    def snapshot() -> Any:
        """Count current source reads without substituting a stored approval."""
        checks.append(board)
        return board

    dialog = api.dialog.ImpedanceDialog(
        None, config, board, api.preview, snapshot, save_config=saved.append
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    api.finish_preparation(dialog)
    history = dialog.session.config.review_tracking
    entries = dialog.session.spacing_entries
    previews: list[Any] = []

    def preview(entry: Any, **kwargs: Any) -> Any:
        """Keep real image bytes while observing every diagnostic verification."""
        if entry in entries:
            previews.append(entry)
        return api.preview(entry, **kwargs)

    dialog.preview_callback = preview
    checks.clear()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert previews == list(entries)
    assert len(checks) >= len(entries) + 1
    assert len(saved) == 1 and saved[0].reviewed_digest
    assert saved[0].review_tracking == history


def test_batch_restores_current_spacing_without_fabricating_review_events(
    draft_api: SimpleNamespace,
) -> None:
    """Reopening binds exact current bytes to existing consent without a new view."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    calls: list[tuple[Any, ...]] = []

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Restore only the current images actually verified by the native batch."""
        calls.append(required)
        return _spacing_evidence(api, dict.fromkeys(required, api.capture.sha256))

    dialog = api.dialog.ImpedanceDialog(
        None, config, board, api.preview, lambda: board, verify_spacing_captures=verify
    )
    assert not dialog.session.approved
    assert not dialog.session.spacing_capture_digests
    history = dialog.session.config.review_tracking
    dialog._verify_spacing_capture_review()
    assert calls == [dialog.session.spacing_entries]
    assert dialog.session.approved
    assert not dialog.session.pending_spacing_reviews
    assert dialog.session.config.review_tracking == history
    assert dialog.session.config.review_tracking == config.review_tracking


def test_cold_save_waits_for_idle_preparation_without_rendering_spacing_batch(
    draft_api: SimpleNamespace,
) -> None:
    """A pending restore's cheap boundary check must not render all missing concerns."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    calls: list[tuple[Any, ...]] = []
    saved: list[Any] = []

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Observe a potentially expensive native batch before idle has run."""
        calls.append(required)
        return _spacing_evidence(api, dict.fromkeys(required, api.capture.sha256))

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: board,
        save_config=saved.append,
        verify_spacing_captures=verify,
    )
    assert dialog._capture_restore_pending
    history = dialog.session.config.review_tracking
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert calls == []
    assert not saved and not dialog._closing
    assert "still being verified" in dialog.status.GetValue()
    assert dialog.session.config.review_tracking == history


def test_recovered_batch_cannot_reinstate_revoked_workbook_approval(
    draft_api: SimpleNamespace,
) -> None:
    """A later valid PNG batch restores evidence while workbook consent stays revoked."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    current = [False]

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Return the same actual bytes across a failed and recovered boundary check."""
        return _spacing_evidence(
            api, dict.fromkeys(required, api.capture.sha256), current=current[0]
        )

    dialog = api.dialog.ImpedanceDialog(
        None, config, board, api.preview, lambda: board, verify_spacing_captures=verify
    )
    history = dialog.session.config.review_tracking
    dialog._verify_spacing_capture_review()
    assert not dialog.session.workbook_review_current
    assert dialog.session.pending_spacing_reviews
    current[0] = True
    dialog._verify_spacing_capture_review()
    assert not dialog.session.pending_spacing_reviews
    assert not dialog.session.approved
    assert not dialog.session.workbook_review_current
    assert dialog.session.config.review_tracking == history


def _prepare_saved_spacing_approvals(api: SimpleNamespace, dialog: Any) -> None:
    """Deliver idle restore events only until saved approvals can be checked."""
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    limit = len(dialog.preview_pane.sections) + len(dialog.session.spacing_entries) + 1
    for _step in range(limit):
        if not dialog._capture_restore_pending:
            return
        dialog.bindings[api.wx.EVT_IDLE, -1](
            SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
        )
        api.drain()
    pytest.fail("Saved approval verification never settled")


@pytest.mark.parametrize(
    "history_kind", ["missing", "view_only", "wrong_issue", "wrong_input", "policy_1"]
)
def test_save_pending_spacing_draft_does_not_capture_unrestorable_history(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    history_kind: str,
) -> None:
    """New or stale diagnostics cannot force eager rendering to save ordinary intent."""
    import importlib

    api = draft_api
    policies = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    policy = policies.SPACING_RENDER_POLICY_REVISION
    if history_kind == "policy_1":
        monkeypatch.setattr(policies, "SPACING_RENDER_POLICY_REVISION", 1)
    config, board = _approved_separate_spacing_concerns(api)
    if history_kind == "policy_1":
        monkeypatch.setattr(policies, "SPACING_RENDER_POLICY_REVISION", policy)
    else:
        records = config.review_tracking.spacing_images
        if history_kind == "missing":
            records = ()
        elif history_kind == "view_only":
            records = tuple(
                replace(record, approved_digest="", approved_at_utc="")
                for record in records
            )
        else:
            field = "issue_id" if history_kind == "wrong_issue" else "input_digest"
            records = tuple(replace(record, **{field: "b" * 64}) for record in records)
        config = replace(
            config,
            review_tracking=replace(config.review_tracking, spacing_images=records),
        )
    saved: list[Any] = []
    batches: list[Any] = []
    diagnostic_captures: list[Any] = []

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Reject native work for records already known not to authorize Save."""
        batches.append(required)
        raise ValueError("Unapproved diagnostic rasterization is unnecessary")

    def preview(entry: Any, **kwargs: Any) -> Any:
        """Preserve ordinary PNG verification while detecting eager diagnostic work."""
        if not isinstance(entry, api.model.Section):
            diagnostic_captures.append(entry)
        return api.preview(entry, **kwargs)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        preview,
        lambda: board,
        save_config=saved.append,
        verify_spacing_captures=verify,
    )
    _prepare_saved_spacing_approvals(api, dialog)
    assert dialog.session.workbook_review_current
    assert diagnostic_captures == [dialog.session.spacing_entries[0]]
    diagnostic_captures.clear()
    history = dialog.session.config.review_tracking
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert batches == []
    assert diagnostic_captures == []
    assert len(saved) == 1 and not saved[0].reviewed_digest
    assert saved[0].review_tracking == history
    assert (
        saved[0].review_tracking.spacing_images == config.review_tracking.spacing_images
    )


def test_save_mixed_spacing_history_verifies_only_existing_current_approvals(
    draft_api: SimpleNamespace,
) -> None:
    """Pending images stay pending while the approved subset gets fresh exact bytes."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    config = replace(
        config,
        review_tracking=replace(
            config.review_tracking,
            spacing_images=config.review_tracking.spacing_images[:1],
        ),
    )
    saved: list[Any] = []
    batches: list[Any] = []
    diagnostic_captures: list[Any] = []

    def verify(_snapshot: Any, required: tuple[Any, ...]) -> Any:
        """Return actual bytes solely for the subset that can restore saved consent."""
        batches.append(required)
        return _spacing_evidence(api, dict.fromkeys(required, api.capture.sha256))

    def preview(entry: Any, **kwargs: Any) -> Any:
        """Restore saved consent before spending idle turns on unapproved images."""
        if not isinstance(entry, api.model.Section):
            diagnostic_captures.append(entry)
            if len(diagnostic_captures) > 1:
                assert entry.view_id == config.review_tracking.spacing_images[0].view_id
        return api.preview(entry, **kwargs)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        preview,
        lambda: board,
        save_config=saved.append,
        verify_spacing_captures=verify,
    )
    _prepare_saved_spacing_approvals(api, dialog)
    assert diagnostic_captures[0] == dialog.session.spacing_entries[0]
    assert len(diagnostic_captures) <= 2
    assert dialog.session.workbook_review_current
    entries = dialog.session.spacing_entries
    expected = tuple(
        entry
        for entry in entries
        if entry.view_id == config.review_tracking.spacing_images[0].view_id
    )
    assert len(expected) == 1 and len(entries) > 1
    history = dialog.session.config.review_tracking
    batches.clear()
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert batches == [expected]
    assert len(saved) == 1 and not saved[0].reviewed_digest
    assert saved[0].review_tracking == history
    assert not dialog.session.approved
    assert any(
        entry not in expected for entry in dialog.session.pending_spacing_reviews
    )


@pytest.mark.parametrize("failure", [None, "source", "palette", "missing"])
def test_cold_spacing_restore_can_exceed_thirty_seconds_without_new_consent(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    failure: Optional[str],
) -> None:
    """Keep cold evidence pending until every idle capture verifies, regardless of time."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api, count=5)
    clock = SimpleNamespace(seconds=0.0)
    state = SimpleNamespace(snapshot=board, palette="initial", failure=None)
    capture_calls: list[Any] = []
    prepared: list[Any] = []
    events: list[Any] = []
    saved: list[Any] = []
    monkeypatch.setattr(
        api.dialog, "time", SimpleNamespace(monotonic=lambda: clock.seconds)
    )

    def prepare(expected: Any, entry: Any) -> Any:
        """Advance virtual work time while returning actual immutable fixture PNGs."""
        assert expected == board
        capture_calls.append(entry)
        if not isinstance(entry, api.model.Section):
            clock.seconds += 10.0
            prepared.append(entry)
            if state.failure == "source":
                state.snapshot = replace(
                    board, context_digest="Changed during cold restore"
                )
            elif state.failure == "palette":
                state.palette = "Changed during cold restore"
            elif state.failure == "missing":
                raise ValueError("The cold spacing PNG is unavailable")
        return api.preview(entry)

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        api.preview,
        lambda: state.snapshot,
        appearance_context=lambda: state.palette,
        prepare_capture=prepare,
        save_config=saved.append,
    )
    dialog.on_screen = True
    dialog._on_main_paint(SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    entries = dialog.session.spacing_entries
    assert len(entries) >= 4
    cold_entries = tuple(
        entry for entry in entries if dialog.preview_pane.image_digest(entry) is None
    )
    assert len(cold_entries) >= 4
    history = dialog.session.config.review_tracking
    viewed = dialog.preview_pane._spacing_image_viewed

    def record_view(entry: Any, image_hash: str, viewed_at: str) -> None:
        """Observe genuine display callbacks without replacing their behavior."""
        events.append((entry, image_hash, viewed_at))
        assert viewed is not None
        viewed(entry, image_hash, viewed_at)

    monkeypatch.setattr(dialog.preview_pane, "_spacing_image_viewed", record_view)

    def idle_once() -> None:
        """Deliver one bound idle event, preserving the one-image work limit."""
        before = len(capture_calls)
        dialog.bindings[api.wx.EVT_IDLE, -1](
            SimpleNamespace(Skip=lambda: None, RequestMore=lambda: None)
        )
        api.drain()
        assert len(capture_calls) - before <= 1

    for _step in range(len(dialog.preview_pane.entries) + 1):
        if len(prepared) == 3:
            break
        idle_once()
    assert clock.seconds == 30.0
    assert len(prepared) == 3
    assert dialog._capture_restore_pending
    assert not dialog.session.approved
    assert dialog.session.pending_spacing_reviews
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert not saved and not dialog._closing
    assert "still being verified" in dialog.status.GetValue()
    assert len(prepared) == 3
    assert dialog.session.config.review_tracking == history

    for _step in range(len(cold_entries) - 4):
        idle_once()
        assert dialog._capture_restore_pending
        assert not dialog.session.approved
        assert dialog.session.config.review_tracking == history
        assert events == []
    state.failure = failure
    idle_once()
    assert clock.seconds == 10.0 * len(cold_entries)
    assert len(prepared) == len(cold_entries)
    assert dialog.session.config.review_tracking == history
    assert history.spacing_images == config.review_tracking.spacing_images
    assert events == []
    if failure is None:
        assert not dialog._capture_restore_pending
        assert not dialog.session.pending_spacing_reviews
        assert dialog.session.approved
    else:
        assert not dialog.session.approved
        assert not dialog.session.workbook_review_current
        assert dialog.session.pending_spacing_reviews
        clock.seconds += 300.0
        dialog._populate_review_status()
        assert not dialog.session.approved
        assert dialog.session.pending_spacing_reviews
        assert dialog.session.config.review_tracking == history
        assert events == []
