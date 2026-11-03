"""Keep spacing failures and temporary edits from stranding persisted intent."""

from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from .test_impedance_calculation_completion import _approved_board, _result
from .test_impedance_spacing_dialog import _config, _gap_board
from .test_impedance_spacing_image_approval import (
    STAMP,
    _approved_separate_spacing_concerns,
    _large_viewer_harness,  # noqa: F401 -- real modal approval boundary
    _no_catalog_workers,  # noqa: F401 -- keep unrelated network work offline
    _show,
    constructor_api,  # noqa: F401 -- required by draft_api
    draft_api as _draft_api,
)

draft_api = _draft_api


@pytest.mark.parametrize("failure", ["unframeable", "budget"])
def test_main_spacing_derivation_failure_is_an_editable_draft(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Real derivation failure cannot prevent opening, Save, or later recovery."""
    api = draft_api
    module = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    config = _config(api)
    board = _gap_board(api, 4_000_000 if failure == "unframeable" else 250_000)
    if failure == "budget":
        monkeypatch.setattr(module, "MAX_SPACING_REVIEW_IMAGES", 0)
    saved: list[Any] = []
    dialog = api.dialog.ImpedanceDialog(
        None, config, board, api.preview, lambda: board, save_config=saved.append
    )
    assert dialog.session.analysis.sections
    assert dialog.sections.GetItemCount() == len(dialog.session.analysis.sections)
    assert "spacing review unavailable" in dialog.status.GetValue().lower()
    assert not dialog.session.approved
    assert not dialog.approve_button.enabled
    with pytest.raises(ValueError, match="Spacing review unavailable"):
        dialog.session.approve()
    with pytest.raises(ValueError):
        module.spacing_review_entries(config, board, dialog.session.analysis.sections)
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert len(saved) == 1 and not saved[0].reviewed_digest
    assert saved[0].specifications == config.specifications
    if failure == "budget":
        monkeypatch.setattr(module, "MAX_SPACING_REVIEW_IMAGES", 256)
    fixed = _gap_board(api, 250_000)
    reopened = api.dialog.ImpedanceDialog(
        None, saved[0], fixed, api.preview, lambda: fixed
    )
    assert reopened.session.spacing_entries
    assert not reopened.session.spacing_review_error


def test_default_spacing_image_budget_rejects_257_real_concerns(
    draft_api: SimpleNamespace,
) -> None:
    """Actual separate pair measurements exceed the unchanged production budget."""
    api = draft_api
    module = importlib.import_module(api.dialog.__package__ + ".spacing_review")
    assert module.MAX_SPACING_REVIEW_IMAGES == 256
    config, base = _config(api), _gap_board(api, 250_000)
    traces = tuple(
        replace(
            trace,
            trace_id=f"budget-{index}-{trace.trace_id}",
            net=f"{trace.net}-{index}",
            points=tuple((x + index * 4_000_000, y) for x, y in trace.points),
        )
        for index in range(257)
        for trace in base.traces[:2]
    )
    board = replace(
        base,
        traces=traces,
        differential_pairs=tuple(
            (traces[index].net, traces[index + 1].net)
            for index in range(0, len(traces), 2)
        ),
        net_class_memberships=tuple(
            (trace.net, (config.specifications[0].net_class,)) for trace in traces
        ),
    )
    sections = api.dialog.analyze(config, board).sections
    assert len(sections) == 257
    with pytest.raises(ValueError, match="More than 256 spacing review images"):
        module.spacing_review_entries(config, board, sections)


def test_unframeable_refresh_replaces_old_rows_and_recovers(
    draft_api: SimpleNamespace,
) -> None:
    """A changed open board cannot leave its old selection or pixels current."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    dialog = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    old_rows = dialog.session.analysis.sections
    changed = _gap_board(api, 4_000_000)
    assert dialog._refresh_rows(changed)
    assert dialog.session.snapshot == changed
    assert dialog.session.analysis.sections != old_rows
    assert dialog._row_ids == tuple(
        section.section_id for section in dialog.session.analysis.sections
    )
    assert not dialog.preview_pane.spacing_entries
    assert not dialog.session.workbook_review_current
    assert dialog.session.spacing_review_error
    assert "spacing review unavailable" in dialog.status.GetValue().lower()
    assert dialog._refresh_rows(board)
    assert dialog.session.spacing_entries
    assert not dialog.session.spacing_review_error
    assert not dialog.session.approved


@pytest.mark.parametrize("correction", ["exclude", "target"])
def test_unframeable_main_child_correction_applies_saves_and_reopens(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, correction: str
) -> None:
    """Repair unavailable diagnostics through the actual child editing workflow."""
    api = draft_api
    board = _gap_board(api, 4_000_000)
    saved: list[Any] = []
    parent = api.dialog.ImpedanceDialog(
        None, _config(api), board, api.preview, lambda: board, save_config=saved.append
    )
    parent.on_screen = True
    assert parent.session.spacing_review_error
    children: list[Any] = []

    def correct_then_apply(child: Any) -> int:
        """Construct, edit, and Apply without approving unavailable close-ups."""
        child.on_screen = True
        children.append(child)
        assert (
            "spacing review unavailable" in child.spacing_comparison.GetLabel().lower()
        )
        if correction == "exclude":
            child.include_layer.SetValue(False)
            child.include_layer.emit(api.wx.EVT_CHECKBOX)
        else:
            child.spacing.SetValue("4")
            child.spacing.emit(api.wx.EVT_TEXT)
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        assert child.modal_result == api.wx.ID_OK, child.validation_message.GetLabel()
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", correct_then_apply, raising=False
    )
    parent._edit_specification(0)
    assert len(children) == 1
    assert not parent.session.spacing_review_error
    assert not parent.session.approved
    specification = parent.session.config.specifications[0]
    if correction == "exclude":
        assert specification.excluded_layers == ("F.Cu",)
        assert not specification.layer_settings
    else:
        assert specification.layer_settings[0].spacing_nm == 4_000_000
    parent.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert parent.modal_result == api.wx.ID_OK and len(saved) == 1
    assert not saved[0].reviewed_digest
    persisted = api.model.Config.from_dict(saved[0].to_dict())
    reopened = api.dialog.ImpedanceDialog(
        None, persisted, board, api.preview, lambda: board
    )
    assert reopened.session.config.specifications == (specification,)
    assert not reopened.session.spacing_review_error
    assert not reopened.session.pending_spacing_reviews
    assert not reopened.session.approved


def _approved_with_stackup(api: SimpleNamespace) -> tuple[Any, Any]:
    """Persist differential approval with accepted calculator context and pixels."""
    config, board = _approved_separate_spacing_concerns(api)
    base, _board = _approved_board(api, prior_results=False)
    session = api.dialog.ReviewSession(replace(config, stackup=base.stackup), board)
    session.refresh()
    session.spacing_capture_digests.update(
        (
            entry.view_id,
            api.dialog.tracking.spacing_capture_fingerprint(
                entry.input_digest, api.capture.sha256
            ),
        )
        for entry in session.spacing_entries
    )
    session.approve()
    return session.save_result(), board


def test_failed_width_batch_preserves_approval_while_spacing_restore_is_pending(
    draft_api: SimpleNamespace,
) -> None:
    """Pending diagnostic rehashing is not a reason to persist unavailable widths."""
    api = draft_api
    config, board = _approved_with_stackup(api)
    dialog = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    assert dialog.session.workbook_review_current and not dialog.session.approved
    results = tuple(
        _result(api, config.stackup, spec, settings.layer, status="unavailable")
        for spec in config.specifications
        for settings in spec.layer_settings
    )
    dialog._finish_calculation(
        dialog._calculation_generation,
        dialog._calculation_key(dialog.session.config, board),
        results,
        "",
    )
    assert dialog.session.config.width_results == config.width_results
    assert dialog.session.workbook_review_current
    assert not dialog.session.approved


def test_unchanged_child_apply_preserves_parent_pending_spacing_approval(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An actual immediate Apply retains consent without fabricating warm pixels."""
    api = draft_api
    config, board = _approved_with_stackup(api)
    parent = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    assert parent.session.workbook_review_current and not parent.session.approved

    def apply(child: Any) -> int:
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", apply, raising=False
    )
    parent._edit_specification(0)
    assert parent.session.workbook_review_current
    assert parent.session.config.width_results == config.width_results
    assert not parent.session.approved


def test_child_calculator_failure_keeps_pending_parent_approval_without_layer_history(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened workbook's accepted width context survives an advisory failure."""
    api = draft_api
    config, board = _approved_with_stackup(api)
    config = replace(config, review_tracking=replace(config.review_tracking, layers=()))
    parent = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    assert parent.session.workbook_review_current and not parent.session.approved

    def fail_then_apply(child: Any) -> int:
        spec, digest = child._draft_calculation_input()
        assert spec is not None and digest
        unavailable = _result(api, config.stackup, spec, "F.Cu", status="unavailable")
        child._finish_draft_calculation(
            child._draft_calc_generation, digest, unavailable, ""
        )
        assert "unavailable" in child.width_hint.GetLabel().lower()
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", fail_then_apply, raising=False
    )
    parent._edit_specification(0)
    assert parent.session.workbook_review_current
    assert parent.session.config.width_results == config.width_results


def test_temporary_target_view_does_not_erase_original_approval(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Viewing a different draft target then reverting preserves original consent."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    parent = api.dialog.ImpedanceDialog(None, config, board, api.preview, lambda: board)
    parent.on_screen = True

    def edit(child: Any) -> int:
        child.on_screen = True
        original = tuple(child.preview_pane.spacing_entries)
        child.spacing.SetValue("0.201")
        child.spacing.emit(api.wx.EVT_TEXT)
        different = tuple(child.preview_pane.spacing_entries)
        assert different and different[0].input_digest != original[0].input_digest
        _show(api, child, 0)
        from .test_impedance_netclass_dialog import _drive_spacing_viewer

        with monkeypatch.context() as modal_patch:
            modal_patch.setattr(
                api.wx.Dialog,
                "ShowModal",
                lambda viewer: _drive_spacing_viewer(api, viewer, approve=False),
            )
            child.approve_layer.emit(api.wx.EVT_BUTTON)
        assert any(
            record.input_digest == different[0].input_digest
            for record in child.review_tracking.spacing_images
        ), child.validation_message.GetLabel()
        child.spacing.SetValue("0.2")
        child.spacing.emit(api.wx.EVT_TEXT)
        assert child.preview_pane.spacing_entries == original
        child.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        return child.modal_result

    monkeypatch.setattr(
        api.dialog.SpecificationDialog, "ShowModal", edit, raising=False
    )
    parent._edit_specification(0)
    parent._verify_spacing_capture_review()
    assert not parent.session.pending_spacing_reviews
    assert parent.session.workbook_review_current
    saved: list[Any] = []
    parent.save_config = saved.append
    parent._capture_restore_pending = False
    parent.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert saved and saved[0].reviewed_digest
    persisted = api.model.Config.from_dict(saved[0].to_dict())
    reopened = api.dialog.ImpedanceDialog(
        None, persisted, board, api.preview, lambda: board
    )
    reopened._verify_spacing_capture_review()
    assert reopened.session.approved


def test_target_variants_keep_distinct_history_and_reuse_exact_legacy_approval(
    draft_api: SimpleNamespace,
) -> None:
    """Old geometric view keys restore only their exact input, with current pixels."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api)
    session = api.dialog.ReviewSession(config, board)
    entries = session.spacing_entries
    assert entries and entries[0].legacy_view_id != entries[0].view_id
    legacy = replace(
        config,
        review_tracking=replace(
            config.review_tracking,
            spacing_images=tuple(
                replace(
                    record,
                    view_id=next(
                        entry.legacy_view_id
                        for entry in entries
                        if entry.view_id == record.view_id
                    ),
                )
                for record in config.review_tracking.spacing_images
            ),
        ),
    )
    opened = api.dialog.ImpedanceDialog(None, legacy, board, api.preview, lambda: board)
    opened._verify_spacing_capture_review()
    assert opened.session.approved
    spec = config.specifications[0]
    changed = replace(
        spec, layer_settings=(replace(spec.layer_settings[0], spacing_nm=201_000),)
    )
    opened.session.replace_specifications((changed,))
    opened.session.refresh()
    for entry in opened.session.spacing_entries:
        opened.session.record_spacing_view(entry, api.capture.sha256, STAMP)
    assert opened.session.pending_spacing_reviews
    opened.session.replace_specifications((spec,))
    opened.session.refresh()
    opened._verify_spacing_capture_review()
    assert not opened.session.pending_spacing_reviews


def test_spacing_history_retention_stays_bounded_without_losing_approved_target(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated unapproved targets cannot make valid intent impossible to store."""
    api = draft_api
    config, board = _approved_separate_spacing_concerns(api, count=1)
    tracking = api.dialog.tracking
    monkeypatch.setattr(tracking, "MAX_SPACING_IMAGE_RECORDS", 3)
    original = config.review_tracking.spacing_images[0]
    history = config.review_tracking
    for index in range(5):
        history = tracking.put_spacing_image(
            history,
            replace(
                original,
                view_id=f"draft-{index}",
                approved_digest="",
                approved_at_utc="",
            ),
        )
        assert len(history.spacing_images) <= 3
    assert original in history.spacing_images
    assert any(record.view_id == "draft-4" for record in history.spacing_images)
    other = replace(original, spec_id="other")
    merged = tracking.merge_spec(
        tracking.ReviewTracking(spacing_images=(other,)), history, original.spec_id
    )
    assert len(merged.spacing_images) == 3
    assert original in merged.spacing_images and other in merged.spacing_images
    assert api.model.Config.from_dict(
        replace(config, review_tracking=history).to_dict()
    )


def test_legacy_spacing_approval_survives_real_report_preparation_and_writers(
    tmp_path: Path,
) -> None:
    """Legacy history still needs exact current PNGs at both real report gates."""
    from impedance import service
    from impedance.spacing_review import pending_spacing_reviews, spacing_review_entries

    from .test_impedance_export_capture_proof import (
        _approved_spacing_plan,
        _OriginalSpacingRenderer,
    )

    original = _approved_spacing_plan()
    entries = spacing_review_entries(
        original.config, original.snapshot, original.analysis.sections
    )
    legacy = replace(
        original.config,
        review_tracking=replace(
            original.config.review_tracking,
            spacing_images=tuple(
                replace(
                    record,
                    view_id=next(
                        entry.legacy_view_id
                        for entry in entries
                        if entry.view_id == record.view_id
                    ),
                )
                for record in original.config.review_tracking.spacing_images
            ),
        ),
    )
    assert not pending_spacing_reviews(
        legacy, original.snapshot, original.analysis.sections
    )
    plan = service.prepare(legacy, original.snapshot)
    assert plan is not None
    renderer = _OriginalSpacingRenderer()
    result = service.export_reports(
        plan,
        object(),
        object(),
        tmp_path,
        renderer_factory=lambda _board, _pcbnew: renderer,
    )
    assert result.workbook.is_file()
    markup = result.html_report.read_text(encoding="utf-8")
    assert "Not approved for this capture" not in markup
    assert "Approved ·" in markup
    changed = tmp_path / "changed"
    changed.mkdir()
    changed_renderer = _OriginalSpacingRenderer()
    changed_renderer.failure = "pixels"
    with pytest.raises(ValueError, match="changed|review|approve"):
        service.export_reports(
            plan,
            object(),
            object(),
            changed,
            renderer_factory=lambda _board, _pcbnew: changed_renderer,
        )
