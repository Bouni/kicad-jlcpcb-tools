"""Regression tests for issue #847: Part Details window activation and parenting."""

from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from .wx_harness import load, mainwindow_stubs, wx_stubs

_PACKAGE = "partselector_part_details_tests"
_ids = count(1)

_wx = wx_stubs(
    Dialog=type("Dialog", (), {"__init__": lambda self, *args, **kwargs: None}),
    Frame=type("Frame", (), {}),
    NewIdRef=lambda: next(_ids),
    NewId=lambda: next(_ids),
    PostEvent=lambda target, event: None,
)

_stubs = mainwindow_stubs(
    _PACKAGE,
    wx=_wx,
    datamodel={
        "PartSelectorDataModel": Mock(),
        "PartListDataModel": Mock(),
        "STANDARD_ONLY_TOOLTIP": "",
    },
    dataview_highlight={
        "HighlightedTextRenderer": Mock(),
        "decode_highlighted_value": lambda value: (value, []),
        "simplify_footprint_name": lambda value: value,
    },
)
partselector = load(_PACKAGE, "partselector", _stubs)
partdetails = load(_PACKAGE, "partdetails", _stubs)

PartSelectorDialog = partselector.PartSelectorDialog
PartDetailsDialog = partdetails.PartDetailsDialog


def test_partselector_opens_part_details_with_self_as_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PartSelectorDialog.get_part_details must pass self (not self.parent) to PartDetailsDialog."""
    main_frame = SimpleNamespace(
        window=Mock(),
        scale_factor=1.5,
        project_path="/dummy/project",
    )
    selector = object.__new__(PartSelectorDialog)
    selector.parent = main_frame
    selector.part_list = Mock()
    selector.part_list.GetSelectedItemsCount.return_value = 1
    selected_item = Mock()
    selector.part_list.GetSelection.return_value = selected_item
    selector.part_list_model = Mock()
    selector.part_list_model.get_lcsc.return_value = "C25804"

    created_dialogs: list[Mock] = []

    def mock_part_details(parent, part):
        dialog = Mock()
        dialog.parent = parent
        dialog.part = part
        created_dialogs.append(dialog)
        return dialog

    monkeypatch.setattr(partselector, "PartDetailsDialog", mock_part_details)

    selector.get_part_details()

    assert len(created_dialogs) == 1
    dialog = created_dialogs[0]
    assert dialog.part == "C25804"
    assert dialog.parent is selector
    assert dialog.parent is not main_frame
    dialog.Show.assert_called_once_with()


def test_partselector_initializes_context_attributes_from_parent() -> None:
    """PartSelectorDialog stores window, scale_factor, and project_path from parent."""
    parent_window = Mock()
    main_parent = SimpleNamespace(
        window=parent_window,
        scale_factor=2.0,
        project_path="/workspace/kicad_proj",
    )

    selector = object.__new__(PartSelectorDialog)
    selector._init_context(main_parent)

    assert selector.window is parent_window
    assert selector.scale_factor == 2.0
    assert selector.project_path == "/workspace/kicad_proj"


def test_partselector_initializes_context_attributes_fallback() -> None:
    """PartSelectorDialog falls back gracefully when parent lacks layout attributes."""
    bare_parent = SimpleNamespace()

    selector = object.__new__(PartSelectorDialog)
    selector._init_context(bare_parent)

    assert selector.window is bare_parent
    assert selector.scale_factor == 1.0
    assert selector.project_path == ""


def test_partdetails_resolves_attributes_when_parent_is_partselector() -> None:
    """PartDetailsDialog resolves window, scale_factor, and datasheet_path from PartSelectorDialog."""
    grandparent_frame = SimpleNamespace(
        window=Mock(),
        scale_factor=1.25,
        project_path="/home/user/design",
    )
    selector = SimpleNamespace(
        parent=grandparent_frame,
        window=grandparent_frame.window,
        scale_factor=1.25,
        project_path="/home/user/design",
    )

    dialog = object.__new__(PartDetailsDialog)
    dialog._init_context(selector)

    assert dialog.window is grandparent_frame.window
    assert dialog.scale_factor == 1.25
    assert dialog.project_path == "/home/user/design"
    assert dialog.datasheet_path == Path("/home/user/design/datasheets")


def test_partdetails_resolves_project_path_from_grandparent_if_missing_on_parent() -> (
    None
):
    """If parent lacks project_path, PartDetailsDialog climbs to parent.parent."""
    grandparent_frame = SimpleNamespace(
        window=Mock(),
        scale_factor=1.0,
        project_path="/home/user/design",
    )
    selector_without_project_path = SimpleNamespace(
        parent=grandparent_frame,
        window=grandparent_frame.window,
        scale_factor=1.0,
    )

    dialog = object.__new__(PartDetailsDialog)
    dialog._init_context(selector_without_project_path)

    assert dialog.project_path == "/home/user/design"
    assert dialog.datasheet_path == Path("/home/user/design/datasheets")


def test_partdetails_on_close_raises_parent_and_destroys() -> None:
    """PartDetailsDialog._on_close must raise its immediate parent before destroying."""
    parent_mock = Mock()
    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = parent_mock
    dialog.Destroy = Mock()

    PartDetailsDialog._on_close(dialog, None)

    parent_mock.Raise.assert_called_once_with()
    dialog.Destroy.assert_called_once_with()


def test_partdetails_on_close_tolerates_parent_without_raise() -> None:
    """PartDetailsDialog._on_close succeeds even if parent does not support Raise."""
    parent_mock = object()
    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = parent_mock
    dialog.Destroy = Mock()

    PartDetailsDialog._on_close(dialog, None)

    dialog.Destroy.assert_called_once_with()


def test_partdetails_savepdf_climbs_hierarchy_to_post_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Route MessageEvent from savepdf to the nearest ancestor with display_message."""
    posted_events: list[tuple[Any, Any]] = []

    def mock_post_event(target, event):
        posted_events.append((target, event))

    monkeypatch.setattr(partdetails.wx, "PostEvent", mock_post_event)

    main_frame = SimpleNamespace(
        display_message=Mock(),
    )
    selector = SimpleNamespace(
        parent=main_frame,
    )

    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = selector
    dialog.pdfurl = None
    dialog.logger = Mock()

    dialog.savepdf()

    assert len(posted_events) == 1
    target, event = posted_events[0]
    assert target is main_frame
    assert event.title == "Error"


def test_partdetails_savepdf_terminates_on_cyclic_parent_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Datasheet PDF save traversal must terminate cleanly even with circular parent references."""
    posted_events: list[tuple[Any, Any]] = []

    def mock_post_event(target, event):
        posted_events.append((target, event))

    monkeypatch.setattr(partdetails.wx, "PostEvent", mock_post_event)

    parent_a = SimpleNamespace()
    parent_b = SimpleNamespace(parent=parent_a)
    parent_a.parent = parent_b

    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = parent_b
    dialog.pdfurl = None
    dialog.logger = Mock()

    dialog.savepdf()

    assert len(posted_events) == 1
    assert posted_events[0][1].title == "Error"
