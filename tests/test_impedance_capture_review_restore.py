"""Restored workbook approval must describe current captures, not historical PNGs.

Exercise production constructors and bound handlers with stateful wx doubles.
Rendering/visible painting remain explicit; loading a capture is not a view event.
The same regression is separately exercised with native Cocoa controls.
"""

from __future__ import annotations

from dataclasses import replace
import struct
from types import SimpleNamespace
from typing import TYPE_CHECKING
import zlib

import pytest

from tests.test_impedance_dialog import constructor_api as _constructor_api
from tests.test_impedance_draft_dialog import draft_api as _draft_api
from tests.test_impedance_netclass_dialog import view_all_rows

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
    api: SimpleNamespace, *, both_layers: bool = False
) -> tuple[BoardSnapshot, SpecificationDialog]:
    """View all originals and explicitly approve through the real OK handler."""
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
    spec = api.model.Specification(
        "rf",
        "RF",
        "50",
        "single_ended",
        "RF",
        tuple(api.model.LayerSettings(layer, ("In1.Cu",)) for layer in layers),
    )
    old = api.create_dialog(board, spec)
    for _layer in layers:
        view_all_rows(api, old)
        old.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
        api.drain()
        api.paint(old.preview_pane)
    assert old.specification == spec
    assert old.modal_result == api.wx.ID_OK
    return board, old


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
    session.approve()
    return session.save_result()


def _show_main(api: SimpleNamespace, dialog: ImpedanceDialog) -> None:
    """Deliver actual show and visible paint independently of capture loading."""
    dialog.on_screen = True
    event = SimpleNamespace(
        GetEventObject=lambda: dialog, IsShown=lambda: True, Skip=lambda: None
    )
    dialog.bindings[api.wx.EVT_SHOW, -1](event)
    api.drain()
    api.paint(dialog.preview_pane)


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
    assert dialog.review_status.GetLabel() == "Needs approval"
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


@pytest.mark.parametrize("secondary", ["changed", "unavailable", "unchanged"])
def test_restored_workbook_verifies_secondary_captures_without_view_events(
    draft_api: SimpleNamespace,
    secondary: str,
) -> None:
    """Verify all included captures before treating restored approval as current."""
    api = draft_api
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
    assert fetched == ["A", "B"]
    assert dialog.session.approved is (secondary == "unchanged")
    assert dialog.session.config.review_tracking == config.review_tracking
    assert dialog.config is config
    assert dialog.review_status.GetLabel() == (
        "✓ 2 workbook rows approved" if secondary == "unchanged" else "Needs approval"
    )


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
