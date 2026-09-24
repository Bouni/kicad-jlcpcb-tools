"""Keep necessary preview safety boundaries without duplicate pre-render scans."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests import test_impedance_shared_preview as preview_helpers

shared_preview_api = preview_helpers.shared_preview_api
_open_preview = preview_helpers._open_preview
_paint = preview_helpers._paint


def test_capture_is_verified_before_display_and_again_after_visible_paint(
    shared_preview_api: SimpleNamespace,
) -> None:
    """The provider owns render checks; the UI checks install and visible evidence."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    pane.set_sections((api.section,), "board")
    api.drain()

    assert len(state.captures) == 1
    assert state.verifies == 1
    assert not pane.ready and not state.views
    _paint(api, pane)
    assert state.verifies == 2
    assert pane.ready and len(state.views) == 1

    pane.show_selected()
    api.drain()
    _paint(api, pane)
    assert len(state.captures) == 1
    assert state.verifies == 3
    assert len(state.views) == 1


@pytest.mark.parametrize("change_at", ["capture", "paint"])
def test_source_changes_never_produce_review_evidence(
    shared_preview_api: SimpleNamespace, change_at: str
) -> None:
    """Reject stale pixels before installation or before recording their view."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane, state = opened.pane, opened.state
    changed = False

    def verify() -> None:
        if changed:
            raise ValueError("The board changed")

    def capture(_section: object, *, refresh: bool = False) -> object:
        nonlocal changed
        changed = change_at == "capture"
        return api.capture

    pane._verify_current = verify
    pane._preview = capture
    pane.set_sections((api.section,), "board")
    api.drain()
    if change_at == "paint":
        assert pane.canvas._image is not None
        changed = True
        _paint(api, pane)
    assert not pane.ready and not state.views
    assert pane.canvas._image is None
    assert "board changed" in pane.failure


def test_checklist_identifies_current_image_before_capture_finishes(
    shared_preview_api: SimpleNamespace,
) -> None:
    """Navigation identity stays distinct from viewed/todo status."""
    api = shared_preview_api
    opened = _open_preview(api)
    pane = opened.pane
    second = replace(
        api.section, section_id="row-b", net_names=("LONG_NET_NAME_" * 20,)
    )
    pane.set_sections((api.section, second), "board")
    assert pane.checklist.cells[0, 1].startswith("Current · ")
    assert not pane.checklist.cells[1, 1].startswith("Current · ")
    pane.select(1)
    assert not pane.checklist.cells[0, 1].startswith("Current · ")
    assert pane.checklist.cells[1, 1].startswith("Current · ")
    assert pane.checklist.cells[1, 0] == "—"
    assert not pane.ready
