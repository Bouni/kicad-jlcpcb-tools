"""Restored approval must describe current captures, not historical PNGs.

Exercise production constructors and bound handlers with stateful wx doubles.
Rendering/visible painting remain explicit; loading a capture is not a view event.
The same regression is separately exercised with native Cocoa controls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import struct
from types import SimpleNamespace
from typing import TYPE_CHECKING
import zlib

import pytest

from tests.test_impedance_dialog import constructor_api as _constructor_api
from tests.test_impedance_draft_dialog import draft_api as _draft_api
from tests.test_impedance_netclass_dialog import (
    apply_layer,
    choose_layer,
    view_all_rows,
)

constructor_api = _constructor_api
draft_api = _draft_api

if TYPE_CHECKING:
    from impedance.dialog import ImpedanceDialog, SpecificationDialog
    from impedance.model import BoardSnapshot, Config, Section
    from impedance.service import CapturedImage


def _changed_capture(api: SimpleNamespace) -> CapturedImage:
    """Model changed native theme pixels without changing board geometry."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    width, height = 800, 420
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pixels = (b"\0" + b"\x90\x20\x10" * width) * height
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )
    return type(api.capture).from_bytes(data)


def _approved_editor(
    api: SimpleNamespace, *, both_layers: bool = False, covered_stub: bool = False
) -> tuple[BoardSnapshot, SpecificationDialog]:
    """View originals, explicitly approve each layer, then apply through raw ID_OK."""
    layers = ("F.Cu", "B.Cu") if both_layers else ("F.Cu",)
    board = api.model.BoardSnapshot(
        layers=("F.Cu", "In1.Cu", "B.Cu"),
        traces=tuple(
            api.model.Trace(
                f"{layer}-{net}",
                layer,
                net,
                150_000,
                ((0, offset), (2_000_000, offset)),
            )
            for layer in layers
            for net, offset in (("A", 0), ("B", 1_000_000))
        ),
        net_classes=("RF",),
        net_class_memberships=(("A", ("RF",)), ("B", ("RF",))),
        net_class_context_digest="capture-review-fixture",
    )
    if covered_stub:
        board = replace(
            board,
            copper_features=(
                api.model.CopperFeature(
                    "pad-A",
                    "A",
                    "F.Cu",
                    "pad",
                    "rect",
                    (1_000_000, 0),
                    3_000_000,
                    500_000,
                ),
            ),
        )
    spec = api.model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        tuple(api.model.LayerSettings(layer, ("In1.Cu",)) for layer in layers),
    )
    old = api.create_dialog(board, spec)
    for layer in layers:
        choose_layer(api, old, layer)
        view_all_rows(api, old)
        old.preview_pane.select(
            old.preview_pane.entries.index(
                next(
                    section
                    for section in old.preview_pane.sections
                    if not section.exclusion_reason
                )
            )
        )
        apply_layer(api, old)
    assert len(old.review_tracking.layers) == len(layers)
    old.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert old.specification == spec
    assert old.modal_result == api.wx.ID_OK
    return board, old


def _reopen_editor(
    api: SimpleNamespace,
    board: BoardSnapshot,
    old: SpecificationDialog,
    render: Callable[..., CapturedImage],
    *,
    visible: bool = True,
) -> SpecificationDialog:
    """Reopen the actual editor without automatically viewing every capture."""
    result = api.dialog.SpecificationDialog(
        None,
        board,
        old.specification,
        preview=render,
        appearance_context=lambda: "current-native-theme",
        review_tracking=old.review_tracking,
    )
    result.on_screen = visible
    api.drain()
    if visible:
        api.paint(result.preview_pane)
    return result


def _approved_config(
    api: SimpleNamespace, board: BoardSnapshot, old: SpecificationDialog
) -> Config:
    """Approve a workbook containing the genuinely reviewed layer records."""
    config = api.model.Config(
        enabled=True,
        specifications=(old.specification,),
        review_tracking=old.review_tracking,
    )
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    assert session.approve_if_layer_reviews_complete()
    return session.save_result()


def _show_main(api: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Deliver actual show and visible paint independently of capture loading."""
    dialog.on_screen = True
    event = SimpleNamespace(
        GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
    )
    dialog.bindings[api.wx.EVT_SHOW, -1](event)
    dialog.bindings[api.wx.EVT_PAINT, -1](event)
    api.drain()
    api.paint(dialog.preview_pane)
    api.finish_preparation(dialog)


@pytest.mark.parametrize("changed", ["all", "secondary", "other_layer"])
def test_reopened_editor_marks_changed_capture_layer_pending(
    constructor_api: SimpleNamespace, changed: str
) -> None:
    """Neither unchanged first images nor first layers can hide stale captures."""
    api = constructor_api
    board, old = _approved_editor(api, both_layers=True)
    history = old.review_tracking
    new_capture = _changed_capture(api)

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        altered = (
            changed == "all"
            or changed == "secondary"
            and section.net_names == ("B",)
            or changed == "other_layer"
            and section.layer == "B.Cu"
        )
        return new_capture if altered else api.capture

    reopened = _reopen_editor(api, board, old, render, visible=False)
    affected = "B.Cu" if changed == "other_layer" else "F.Cu"
    assert not reopened._layer_drafts[affected].confirmed
    assert f"{affected} — needs review" in reopened.layer.items
    assert reopened.review_tracking == history
    assert old.review_tracking == history


def test_changed_capture_approve_shows_pending_image_then_needs_later_approval(
    constructor_api: SimpleNamespace,
) -> None:
    """Showing the last changed image must not approve it in the same click."""
    api = constructor_api
    board, old = _approved_editor(api)
    previous_approval = old.review_tracking.layers[0]
    new_capture = _changed_capture(api)
    reopened = _reopen_editor(api, board, old, lambda _section, **_kwargs: new_capture)
    assert "needs review" in reopened.preview_pane.checklist.GetItemText(1, 1)
    selected_id = reopened.preview_pane.selected_section.section_id
    reopened.approve_layer.emit(api.wx.EVT_BUTTON)
    assert reopened.modal_result is None
    assert reopened.specification is None
    assert (
        dict(reopened.review_tracking.layers[0].captures)[selected_id]
        != dict(previous_approval.captures)[selected_id]
    )
    api.drain()
    api.paint(reopened.preview_pane)
    assert all(
        "viewed" in label or "approved" in label
        for label in [
            reopened.preview_pane.checklist.GetItemText(index, 1)
            for index in range(reopened.preview_pane.checklist.GetItemCount())
        ]
    )
    assert not reopened._layer_drafts["F.Cu"].confirmed
    reopened.approve_layer.emit(api.wx.EVT_BUTTON)
    assert reopened.modal_result is None
    reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert reopened.modal_result == api.wx.ID_OK
    assert reopened.specification == old.specification
    assert reopened.review_tracking.layers[0].captures != previous_approval.captures
    assert (
        reopened.review_tracking.layers[0].approved_at_utc
        != previous_approval.approved_at_utc
    )


def test_unchanged_restored_layers_keep_approval_and_historical_timestamps(
    constructor_api: SimpleNamespace,
) -> None:
    """Verification may render, but loading must never mint view/approval events."""
    api = constructor_api
    board, old = _approved_editor(api, both_layers=True)
    history = old.review_tracking
    reopened = _reopen_editor(api, board, old, api.preview, visible=False)
    assert not any(draft.confirmed for draft in reopened._layer_drafts.values())
    assert reopened.review_tracking == history
    reopened.on_screen = True
    reopened.bindings[api.wx.EVT_SHOW, -1](
        SimpleNamespace(
            GetEventObject=lambda: reopened, IsShown=lambda: True, Skip=lambda: None
        )
    )
    api.drain()
    api.paint(reopened.preview_pane)
    # Visible paint may update the selected image's last-viewed event. Preparing
    # all remaining images must not create further view or approval events.
    after_paint = reopened.review_tracking
    api.finish_preparation(reopened)
    assert all(draft.confirmed for draft in reopened._layer_drafts.values())
    assert reopened.review_tracking == after_paint
    assert reopened.review_tracking.layers == history.layers
    reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert reopened.modal_result == api.wx.ID_OK
    assert reopened.review_tracking == after_paint
    assert old.review_tracking == history


def test_unavailable_restored_capture_never_authorizes_layer(
    constructor_api: SimpleNamespace,
) -> None:
    """A capture failure on a later image keeps old timestamps historical."""
    api = constructor_api
    board, old = _approved_editor(api)

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        if section.net_names == ("B",):
            raise ValueError("Native capture is unavailable")
        return api.capture

    reopened = _reopen_editor(api, board, old, render)
    assert not reopened._layer_drafts["F.Cu"].confirmed
    reopened.approve_layer.emit(api.wx.EVT_BUTTON)
    api.drain()
    api.paint(reopened.preview_pane)
    reopened.approve_layer.emit(api.wx.EVT_BUTTON)
    assert reopened.modal_result is None
    assert reopened.specification is None
    assert reopened.review_tracking.layers == old.review_tracking.layers
    assert reopened.preview_pane.failure
    reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert reopened.modal_result == api.wx.ID_OK
    assert not reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking.layers == old.review_tracking.layers


@pytest.mark.parametrize("changed_after_show", [False, True])
def test_changed_displayed_capture_revokes_workbook_and_class_approval(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    changed_after_show: bool,
) -> None:
    """Both reopening and explicit refresh must revoke historical approval."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api)
    config = _approved_config(api, board, old)
    saved: list[Config] = []
    state = SimpleNamespace(changed=not changed_after_show)
    new_capture = _changed_capture(api)

    def render(_section: Section, **_kwargs: object) -> CapturedImage:
        return new_capture if state.changed else api.capture

    dialog = api.dialog.ImpedanceDialog(
        None,
        config,
        board,
        render,
        lambda: board,
        appearance_context=lambda: "current-native-theme",
        save_config=saved.append,
    )
    _show_main(api, dialog)
    if changed_after_show:
        assert dialog.session.approved
        state.changed = True
        dialog.preview_pane.select(0, refresh=True)
        api.drain()
        api.paint(dialog.preview_pane)
    assert dialog.preview_pane.ready
    assert not dialog.session.approved
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == "Save draft and close"
    assert dialog.specifications.table_rows[0][0] == "Needs approval"
    assert dialog.session.config.review_tracking.layers == config.review_tracking.layers
    assert dialog.session.config.review_tracking.images != config.review_tracking.images
    assert config == _approved_config(api, board, old)
    skipped: list[bool] = []
    dialog.bindings[api.wx.EVT_BUTTON, api.wx.ID_CANCEL](
        SimpleNamespace(Skip=lambda: skipped.append(True))
    )
    assert skipped == [True]
    assert saved == []
    assert dialog.config is config


def test_parent_does_not_auto_approve_changed_image_history(
    constructor_api: SimpleNamespace,
) -> None:
    """Latest displayed hashes must match those explicitly approved per layer."""
    api = constructor_api
    board, old = _approved_editor(api)
    config = _approved_config(api, board, old)
    tracking = api.dialog.tracking
    first = config.review_tracking.images[0]
    changed = replace(first, capture_digest="a" * 64)
    stale = replace(
        config,
        reviewed_digest="",
        included_section_ids=(),
        review_tracking=tracking.put_image(config.review_tracking, changed),
    )
    session = api.dialog.ReviewSession(stale, board)
    session.refresh()
    assert not session.approve_if_layer_reviews_complete()
    assert not session.approved
    assert session.config.review_tracking.layers == config.review_tracking.layers


@pytest.mark.parametrize("secondary", ["changed", "unavailable", "unchanged"])
def test_restored_workbook_verifies_secondary_captures_without_view_events(
    draft_api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    secondary: str,
) -> None:
    """Verify all included captures before treating restored approval as current."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api)
    config = _approved_config(api, board, old)
    new_capture = _changed_capture(api)
    fetched: list[str] = []

    def render(section: Section, **_kwargs: object) -> CapturedImage:
        fetched.append(section.net_names[0])
        if section.net_names == ("B",):
            if secondary == "unavailable":
                raise ValueError("Native capture is unavailable")
            if secondary == "changed":
                return new_capture
        return api.capture

    dialog = api.dialog.ImpedanceDialog(None, config, board, render, lambda: board)
    assert fetched == []
    assert "verifying" in dialog.review_status.GetLabel().lower()
    dialog.on_screen = True
    dialog.bindings[api.wx.EVT_PAINT, -1](SimpleNamespace(Skip=lambda: None))
    api.drain()
    api.paint(dialog.preview_pane)
    displayed_history = dialog.session.config.review_tracking
    api.finish_preparation(dialog)
    assert set(fetched) == {"A", "B"}
    assert dialog.session.approved is (secondary == "unchanged")
    assert dialog.session.config.review_tracking == displayed_history
    assert displayed_history.layers == config.review_tracking.layers
    assert dialog.config is config
    assert dialog.specifications.table_rows[0][0] == (
        "Approved" if secondary == "unchanged" else "Needs approval"
    )
    assert dialog.FindWindow(api.wx.ID_OK).GetLabel() == (
        "Save and close" if secondary == "unchanged" else "Save draft and close"
    )


def test_optional_stub_inspection_preserves_unchanged_layer_approval(
    constructor_api: SimpleNamespace,
) -> None:
    """An omitted optional stub is not a changed approved capture."""
    api = constructor_api
    board, old = _approved_editor(api, covered_stub=True)
    history = old.review_tracking
    assert len(history.layers[0].captures) == 1
    reopened = _reopen_editor(api, board, old, api.preview)
    assert reopened._layer_drafts["F.Cu"].confirmed
    stub = next(
        section
        for section in reopened.preview_pane.sections
        if section.exclusion_reason
    )
    reopened.preview_pane.select(reopened.preview_pane.entries.index(stub))
    api.drain()
    api.paint(reopened.preview_pane)
    assert reopened._layer_drafts["F.Cu"].confirmed
    reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert reopened.modal_result == api.wx.ID_OK
    assert reopened.review_tracking.layers == history.layers


def test_disabling_stub_skip_requires_a_layer_approval_covering_the_stub(
    constructor_api: SimpleNamespace,
) -> None:
    """Previously optional viewing cannot substitute for explicit layer approval."""
    api = constructor_api
    board, old = _approved_editor(api, covered_stub=True)
    history = old.review_tracking
    reopened = api.dialog.SpecificationDialog(
        None,
        board,
        old.specification,
        preview=api.preview,
        review_tracking=history,
        skip_stub_review=lambda: False,
    )
    assert not reopened._layer_drafts["F.Cu"].confirmed
    assert reopened.review_tracking == history
    reopened.on_screen = True
    api.drain()
    api.paint(reopened.preview_pane)
    view_all_rows(api, reopened)
    assert not reopened._layer_drafts["F.Cu"].confirmed
    apply_layer(api, reopened)
    assert reopened._layer_drafts["F.Cu"].confirmed
    reopened.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert reopened.modal_result == api.wx.ID_OK
    assert len(reopened.review_tracking.layers[0].captures) == 2
    assert reopened.review_tracking.layers != history.layers


def test_failed_save_of_changed_capture_draft_retains_edits_and_history(
    draft_api: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Revocation remains private until persistence succeeds, including retry."""
    api = draft_api
    monkeypatch.setattr(
        api.dialog.ImpedanceDialog,
        "_ensure_catalog_started",
        lambda _self, _count=None: None,
    )
    board, old = _approved_editor(api)
    original = _approved_config(api, board, old)
    new_capture = _changed_capture(api)
    persisted = {"config": original}
    state = SimpleNamespace(fail=True)

    def save(candidate: Config) -> None:
        if state.fail:
            raise ValueError("Database revision changed; retry")
        persisted["config"] = candidate

    dialog = api.dialog.ImpedanceDialog(
        None,
        original,
        board,
        lambda _section, **_kwargs: new_capture,
        lambda: board,
        save_config=save,
    )
    _show_main(api, dialog)
    edited = replace(original.specifications[0], label="Edited label")
    dialog.session.config = replace(dialog.session.config, specifications=(edited,))
    history = dialog.session.config.review_tracking
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result is None
    assert not dialog._closing
    assert persisted["config"] is original
    assert dialog.config is original
    assert dialog.session.config.specifications == (edited,)
    assert dialog.session.config.review_tracking == history
    assert history.layers == original.review_tracking.layers
    assert "Database revision changed" in dialog.status.GetValue()
    state.fail = False
    dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert dialog.modal_result == api.wx.ID_OK
    assert persisted["config"].specifications == (edited,)
    assert persisted["config"].reviewed_digest == ""
    assert persisted["config"].review_tracking == history


def test_accepted_kind_change_replaces_old_current_capture_evidence(
    constructor_api: SimpleNamespace,
) -> None:
    """A genuinely reviewed replacement must not inherit another kind's hashes."""
    api = constructor_api
    board, old = _approved_editor(api)
    config = _approved_config(api, board, old)
    session = api.dialog.ReviewSession(config, board)
    session.refresh()
    session.capture_digests.update(
        (record.section_id, record.capture_digest)
        for record in config.review_tracking.images
    )
    edited = _reopen_editor(api, board, old, api.preview)
    edited.kind.SetSelection(2)
    edited.kind.emit(api.wx.EVT_CHOICE)
    edited.ground_gap.SetValue("0.1")
    apply_layer(api, edited)
    assert edited._layer_drafts["F.Cu"].confirmed
    edited.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    assert edited.modal_result == api.wx.ID_OK
    session.replace_specifications((edited.specification,))
    session.config = replace(
        session.config,
        review_tracking=api.dialog.tracking.merge_spec(
            session.config.review_tracking, edited.review_tracking, "rf"
        ),
    )
    session.refresh()
    assert session.approve_if_layer_reviews_complete()
    assert session.approved
