"""Keep standard-button state local despite duplicate IDs in other dialogs."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Optional

import pytest

from .test_impedance_dialog import _ConstructedControl, _LayoutDouble
from .test_impedance_draft_dialog import (
    _board as _snapshot,
    _intent as _config,
    constructor_api,  # noqa: F401 -- dependency of draft_api fixture
    draft_api,  # noqa: F401 -- localized fixture dependency
)


@pytest.fixture
def hierarchy(
    draft_api: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """Model wx's global static lookup separately from recursive child lookup.

    Installed wx/core.pyi documents FindWindowById as @staticmethod with a
    parent=None global search, whereas FindWindow searches the receiver's
    descendants. Both lookups here return existing controls, never new buttons.
    """
    api = draft_api
    roots: list[_ConstructedControl] = []

    def find_tree(
        node: _ConstructedControl, control_id: int
    ) -> Optional[_ConstructedControl]:
        """Search existing hierarchy nodes in creation order, including the root."""
        if getattr(node, "window_id", None) == control_id:
            return node
        for child in node.children:
            match = find_tree(child, control_id)
            if match is not None:
                return match
        return None

    def find_global(
        control_id: int, parent: Optional[_ConstructedControl] = None
    ) -> Optional[_ConstructedControl]:
        """Reflect the static API: calling through an instance supplies no parent."""
        for root in roots if parent is None else (parent,):
            match = find_tree(root, control_id)
            if match is not None:
                return match
        return None

    def find_child(
        self: _ConstructedControl, control_id: int
    ) -> Optional[_ConstructedControl]:
        """Search only existing descendants, excluding other window hierarchies."""
        for child in self.children:
            match = find_tree(child, control_id)
            if match is not None:
                return match
        return None

    def create_buttons(self: _ConstructedControl, _flags: int) -> _LayoutDouble:
        """Create nested standard buttons before the constructor populates rows."""
        panel = _ConstructedControl(self)
        layout = _LayoutDouble()
        for control_id in (api.wx.ID_OK, api.wx.ID_CANCEL):
            button = _ConstructedControl(panel)
            button.window_id = control_id
            layout.Add(button)
        return layout

    monkeypatch.setattr(
        _ConstructedControl, "FindWindowById", staticmethod(find_global), raising=False
    )
    monkeypatch.setattr(_ConstructedControl, "FindWindow", find_child, raising=False)
    monkeypatch.setattr(_ConstructedControl, "CreateButtonSizer", create_buttons)
    foreign = _ConstructedControl()
    roots.append(foreign)
    foreign.CreateButtonSizer(api.wx.OK | api.wx.CANCEL)
    foreign_ok = foreign.FindWindow(api.wx.ID_OK)
    assert foreign_ok is not None
    return SimpleNamespace(api=api, roots=roots, foreign=foreign, foreign_ok=foreign_ok)


def _open_main(hierarchy: SimpleNamespace) -> _ConstructedControl:
    """Run the real constructor with an older dialog's duplicate OK already live."""
    api = hierarchy.api
    snapshot = _snapshot(api)
    dialog = api.dialog.ImpedanceDialog(
        None, _config(api), snapshot, api.preview, lambda: snapshot
    )
    hierarchy.roots.append(dialog)
    dialog.on_screen = True
    dialog._queue_selected_preview()
    api.drain()
    api.paint(dialog.preview_pane)
    return dialog


def _own_ok(
    hierarchy: SimpleNamespace, dialog: _ConstructedControl
) -> _ConstructedControl:
    """Read the actual child button, independently of the production call site."""
    button = dialog.FindWindow(hierarchy.api.wx.ID_OK)
    assert button is not None
    assert button is not hierarchy.foreign_ok
    return button


@pytest.mark.parametrize("foreign_enabled", [True, False])
def test_opening_unreviewed_dialog_enables_only_its_own_save(
    hierarchy: SimpleNamespace, foreign_enabled: bool
) -> None:
    """Opening or reopening a main dialog cannot change an older dialog's state."""
    hierarchy.foreign_ok.Enable(foreign_enabled)
    first = _open_main(hierarchy)
    first_ok = _own_ok(hierarchy, first)
    assert first_ok.enabled is True
    assert hierarchy.foreign_ok.enabled is foreign_enabled
    first_ok.Enable(False)
    second = _open_main(hierarchy)
    assert _own_ok(hierarchy, second).enabled is True
    assert first_ok.enabled is False
    assert hierarchy.foreign_ok.enabled is foreign_enabled


def test_approve_handler_enables_only_the_reviewed_dialog(
    hierarchy: SimpleNamespace,
) -> None:
    """Explicit approval changes the local button, not another disabled OK."""
    api = hierarchy.api
    dialog = _open_main(hierarchy)
    own_ok = _own_ok(hierarchy, dialog)
    own_ok.Enable(False)
    hierarchy.foreign_ok.Enable(False)

    dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is True
    assert own_ok.enabled is True
    assert hierarchy.foreign_ok.enabled is False
    assert dialog.modal_result is None


def test_child_dialog_ok_is_unchanged_by_parent_review(
    hierarchy: SimpleNamespace,
) -> None:
    """A nested dialog sharing ID_OK is not the parent's Save button."""
    api = hierarchy.api
    parent = _open_main(hierarchy)
    child = _ConstructedControl(parent)
    child.CreateButtonSizer(api.wx.OK | api.wx.CANCEL)
    parent_ok = _own_ok(hierarchy, parent)
    child_ok = _own_ok(hierarchy, child)
    assert parent_ok is not child_ok
    parent_ok.Enable(False)
    child_ok.Enable(False)
    hierarchy.foreign_ok.Enable(True)

    parent.approve_button.emit(api.wx.EVT_BUTTON)

    assert parent.session.approved is True
    assert parent_ok.enabled is True
    assert child_ok.enabled is False
    assert hierarchy.foreign_ok.enabled is True
    parent._refresh_rows()
    assert parent_ok.enabled is True
    assert child_ok.enabled is False
    assert hierarchy.foreign_ok.enabled is True
    assert parent.modal_result is None
    assert child.modal_result is None


@pytest.mark.parametrize(
    "boundary",
    ["refresh", "include", "save", "render", "snapshot"],
)
def test_revoking_approval_keeps_local_draft_save_available(
    hierarchy: SimpleNamespace, boundary: str
) -> None:
    """Review invalidation leaves draft Save usable without touching another dialog."""
    api = hierarchy.api
    dialog = _open_main(hierarchy)
    own_ok = _own_ok(hierarchy, dialog)
    dialog.approve_button.emit(api.wx.EVT_BUTTON)
    assert dialog.session.approved is True
    # Establish two enabled buttons independently so failures isolate revocation,
    # rather than inheriting the separate constructor/approval lookup defect.
    own_ok.Enable(True)
    hierarchy.foreign_ok.Enable(True)
    original_config = dialog.config
    assert dialog.preview_pane.canvas._image is not None

    def changed_board(*_args: object) -> None:
        """Reject stale review without changing the saved configuration."""
        raise ValueError("The board changed; rescan and review its sections.")

    def failed_capture(_section: object, *, refresh: bool = False) -> object:
        """Fail the production callback after an earlier image was visible."""
        raise ValueError("The native PCB capture failed; rescan and review.")

    if boundary == "refresh":
        dialog._refresh_rows(
            replace(dialog.session.snapshot, context_digest="changed board text")
        )
    elif boundary == "include":
        dialog.sections.Check(0, False)
        dialog.sections.emit(api.wx.EVT_CHECKLISTBOX)
    elif boundary == "save":
        dialog.refresh_snapshot = changed_board
        dialog.emit(api.wx.EVT_BUTTON, api.wx.ID_OK)
    elif boundary == "render":
        dialog.preview_pane._preview = failed_capture
        dialog.preview_pane.select(0, refresh=True)
        api.drain()
    elif boundary == "snapshot":
        dialog.refresh_snapshot = changed_board
        dialog.approve_button.emit(api.wx.EVT_BUTTON)

    assert dialog.session.approved is False
    assert own_ok.enabled is True
    assert hierarchy.foreign_ok.enabled is True
    assert dialog.config is original_config
    assert dialog.modal_result is None
    if boundary in ("render", "snapshot"):
        assert dialog.preview_pane.canvas._image is None
        assert dialog.preview_pane.canvas._bitmap is None
        assert "rescan and review" in dialog.status.GetValue()
    if boundary == "save":
        assert "rescan and review" in dialog.status.GetValue()
