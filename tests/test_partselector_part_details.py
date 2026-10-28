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
    dialog.datasheet_file_id = None
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
    dialog.datasheet_file_id = None
    dialog.logger = Mock()

    dialog.savepdf()

    assert len(posted_events) == 1
    assert posted_events[0][0] is parent_b
    assert posted_events[0][1].title == "Error"


def test_partdetails_savepdf_handles_empty_string_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty string pdfurl must post an error without raising IndexError."""
    posted_events: list[tuple[Any, Any]] = []

    def mock_post_event(target, event):
        posted_events.append((target, event))

    monkeypatch.setattr(partdetails.wx, "PostEvent", mock_post_event)

    main_frame = SimpleNamespace(display_message=Mock())
    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = main_frame
    dialog.pdfurl = ""
    dialog.datasheet_file_id = ""
    dialog.logger = Mock()

    dialog.savepdf()

    assert len(posted_events) == 1
    assert posted_events[0][0] is main_frame
    assert posted_events[0][1].title == "Error"
    assert posted_events[0][1].text == "Undefined URL for datasheet download"


def test_partdetails_savepdf_falls_back_to_parent_when_no_ancestor_has_display_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no ancestor implements display_message, fall back safely to self.parent."""
    posted_events: list[tuple[Any, Any]] = []

    def mock_post_event(target, event):
        posted_events.append((target, event))

    monkeypatch.setattr(partdetails.wx, "PostEvent", mock_post_event)

    grandparent = SimpleNamespace()
    parent = SimpleNamespace(parent=grandparent)
    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = parent
    dialog.pdfurl = None
    dialog.datasheet_file_id = None
    dialog.logger = Mock()

    dialog.savepdf()

    assert len(posted_events) == 1
    assert posted_events[0][0] is parent
    assert posted_events[0][1].title == "Error"


JLC_DATASHEET = (
    "https://jlcpcb.com/api/file/downloadByFileSystemAccessId/8579706381320323072"
)
LCSC_DATASHEET = "https://www.lcsc.com/datasheet/lcsc_datasheet_C25804.pdf"


def _saving_dialog(
    tmp_path: Path,
    *,
    file_id: Any,
    pdfurl: Any,
    results: list[dict[str, Any]],
) -> Any:
    """Return a Part Details dialog whose downloads answer with ``results``."""
    dialog = object.__new__(PartDetailsDialog)
    dialog.parent = SimpleNamespace(display_message=Mock())
    dialog.part = "C25804"
    dialog.pdfurl = pdfurl
    dialog.datasheet_file_id = file_id
    dialog.datasheet_path = tmp_path / "datasheets"
    dialog.logger = Mock()
    dialog.lcsc_api = Mock()
    dialog.lcsc_api.download_datasheet.side_effect = results
    return dialog


def _posted(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Collect the message events the dialog posts."""
    posted: list[Any] = []
    monkeypatch.setattr(
        partdetails.wx, "PostEvent", lambda _target, event: posted.append(event)
    )
    return posted


def test_partdetails_savepdf_downloads_the_jlc_file_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The datasheet's JLCPCB file is the PDF; LCSC's link is not needed then."""
    posted = _posted(monkeypatch)
    dialog = _saving_dialog(
        tmp_path,
        file_id="8579706381320323072",
        pdfurl=LCSC_DATASHEET,
        results=[{"success": True, "msg": "Saved"}],
    )

    dialog.savepdf()

    dialog.lcsc_api.download_datasheet.assert_called_once_with(
        JLC_DATASHEET, tmp_path / "datasheets" / "lcsc_datasheet_C25804.pdf"
    )
    assert [(event.title, event.text) for event in posted] == [("Success", "Saved")]


def test_partdetails_savepdf_falls_back_to_the_lcsc_link(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """LCSC's link is tried when the JLCPCB file gives no PDF."""
    posted = _posted(monkeypatch)
    dialog = _saving_dialog(
        tmp_path,
        file_id="8579706381320323072",
        pdfurl=LCSC_DATASHEET,
        results=[
            {"success": False, "msg": "The datasheet link did not return a PDF."},
            {"success": True, "msg": "Saved"},
        ],
    )

    dialog.savepdf()

    assert [
        call.args[0] for call in dialog.lcsc_api.download_datasheet.call_args_list
    ] == [JLC_DATASHEET, LCSC_DATASHEET]
    assert [(event.title, event.text) for event in posted] == [("Success", "Saved")]


def test_partdetails_savepdf_names_a_datasheet_without_a_link_after_the_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With only a file id the PDF is named after the part, and a failure shows."""
    posted = _posted(monkeypatch)
    dialog = _saving_dialog(
        tmp_path,
        file_id="8579706381320323072",
        pdfurl=None,
        results=[{"success": False, "msg": "non-OK HTTP response status"}],
    )

    dialog.savepdf()

    dialog.lcsc_api.download_datasheet.assert_called_once_with(
        JLC_DATASHEET, tmp_path / "datasheets" / "C25804.pdf"
    )
    assert [(event.title, event.text) for event in posted] == [
        ("Error", "non-OK HTTP response status")
    ]


def test_partdetails_savepdf_reports_the_first_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When both sources fail, JLCPCB's error shows, not LCSC's expected one."""
    posted = _posted(monkeypatch)
    dialog = _saving_dialog(
        tmp_path,
        file_id="8579706381320323072",
        pdfurl=LCSC_DATASHEET,
        results=[
            {"success": False, "msg": "Failed to download datasheet: timed out"},
            {"success": False, "msg": "The datasheet link did not return a PDF."},
        ],
    )

    dialog.savepdf()

    assert [(event.title, event.text) for event in posted] == [
        ("Error", "Failed to download datasheet: timed out")
    ]


@pytest.mark.parametrize(
    ("record", "enabled"),
    [
        (
            {
                "dataManualUrl": LCSC_DATASHEET,
                "dataManualFileAccessId": "8579706381320323072",
            },
            True,
        ),
        ({"dataManualFileAccessId": "8579706381320323072"}, True),
        ({"dataManualUrl": LCSC_DATASHEET}, True),
        ({}, False),
    ],
    ids=["both", "file-id-only", "link-only", "neither"],
)
def test_partdetails_part_record_names_the_datasheet_to_download(
    record: dict[str, str], enabled: bool
) -> None:
    """Download keeps the record's file id and link, and is enabled by either."""
    dialog = object.__new__(PartDetailsDialog)
    dialog.data_list = Mock()
    dialog.savepdf_button = Mock()
    dialog.openpdf_button = Mock()
    dialog.openpage_button = Mock()

    dialog._apply_part_data({"success": True, "data": {"data": record}}, None)

    assert dialog.pdfurl == record.get("dataManualUrl")
    assert dialog.datasheet_file_id == record.get("dataManualFileAccessId")
    dialog.savepdf_button.Enable.assert_called_once_with(enabled)


@pytest.mark.parametrize(
    ("pdfurl", "filename"),
    [
        (LCSC_DATASHEET, "lcsc_datasheet_C25804.pdf"),
        ("/", "C25804.pdf"),
        ("https://www.findernet.com/en/products/series-95/", "C25804.pdf"),
        ("https://uk.rs-online.com/web/p/5442601?cm_mmc=aff-_-cn", "C25804.pdf"),
        ("https://www.lcsc.com/datasheet/SETsafe--|--SETfuse_C25804.pdf", "C25804.pdf"),
    ],
    ids=["pdf-name", "slash", "trailing-slash", "query", "reserved-character"],
)
def test_partdetails_savepdf_keeps_lcsc_names_only_when_they_are_file_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pdfurl: str, filename: str
) -> None:
    """A link that doesn't end in a plain PDF name saves as <part>.pdf."""
    _posted(monkeypatch)
    dialog = _saving_dialog(
        tmp_path,
        file_id="8579706381320323072",
        pdfurl=pdfurl,
        results=[{"success": True, "msg": "Saved"}],
    )

    dialog.savepdf()

    dialog.lcsc_api.download_datasheet.assert_called_once_with(
        JLC_DATASHEET, tmp_path / "datasheets" / filename
    )


def test_partdetails_download_is_enabled_when_a_row_fails_to_display() -> None:
    """The datasheet is taken before the rows, so a bad row cannot lose it."""
    dialog = object.__new__(PartDetailsDialog)
    dialog.data_list = Mock()
    dialog.data_list.AppendItem.side_effect = TypeError("bad value")
    dialog.savepdf_button = Mock()
    dialog.openpdf_button = Mock()
    dialog.openpage_button = Mock()

    with pytest.raises(TypeError):
        dialog._apply_part_data(
            {
                "success": True,
                "data": {
                    "data": {
                        "componentCode": "C25804",
                        "dataManualFileAccessId": "8579706381320323072",
                    }
                },
            },
            None,
        )

    assert dialog.datasheet_file_id == "8579706381320323072"
    dialog.savepdf_button.Enable.assert_called_once_with(True)


def test_partdetails_savepdf_reports_a_datasheets_folder_it_cannot_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The button doesn't touch the folder itself, so a bad one is reported, not raised."""
    posted = _posted(monkeypatch)
    (tmp_path / "datasheets").write_text("not a folder")
    dialog = _saving_dialog(
        tmp_path,
        file_id="8579706381320323072",
        pdfurl=None,
        results=[{"success": False, "msg": "Could not save the datasheet"}],
    )

    dialog.savepdf()

    assert [(event.title, event.text) for event in posted] == [
        ("Error", "Could not save the datasheet")
    ]
