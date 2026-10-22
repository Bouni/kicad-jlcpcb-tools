"""Part Details opens its links through wx, once the part record has them."""

from typing import Any
from unittest.mock import Mock

import pytest

from .wx_harness import load, mainwindow_stubs, wx_stubs

_PACKAGE = "partdetails_links_tests"
_stubs = mainwindow_stubs(
    _PACKAGE,
    wx=wx_stubs(
        Dialog=type("Dialog", (), {"__init__": lambda self, *args, **kwargs: None})
    ),
)
partdetails = load(_PACKAGE, "partdetails", _stubs)
PartDetailsDialog = partdetails.PartDetailsDialog

DATASHEET = "https://www.lcsc.com/datasheet/lcsc_datasheet_C25804.pdf"
PAGE = "https://www.lcsc.com/product-detail/C25804.html"


def _dialog() -> Any:
    """Return a Part Details dialog for C25804 with recording controls."""
    dialog = object.__new__(PartDetailsDialog)
    dialog.part = "C25804"
    dialog.pdfurl = DATASHEET
    dialog.pageurl = PAGE
    dialog.logger = Mock()
    dialog.data_list = Mock()
    dialog.openpdf_button = Mock()
    dialog.openpage_button = Mock()
    return dialog


class _LogNull:
    """Stand in for wx.LogNull, recording whether wx's log is silenced."""

    active = False

    def __enter__(self) -> "_LogNull":
        type(self).active = True
        return self

    def __exit__(self, *_exc: object) -> bool:
        type(self).active = False
        return False


def _browser(
    monkeypatch: pytest.MonkeyPatch, *, opened: bool
) -> tuple[Mock, Mock, list[bool]]:
    """Replace the launch, the message box and wx's log silencer.

    The list returned records, for each launch, whether wx's log was
    silenced while it ran.
    """
    silenced: list[bool] = []

    def launched(_url: str) -> bool:
        silenced.append(_LogNull.active)
        return opened

    launch = Mock(side_effect=launched)
    message = Mock()
    monkeypatch.setattr(partdetails.wx, "LaunchDefaultBrowser", launch, raising=False)
    monkeypatch.setattr(partdetails.wx, "MessageBox", message, raising=False)
    monkeypatch.setattr(partdetails.wx, "LogNull", _LogNull, raising=False)
    return launch, message, silenced


@pytest.mark.parametrize(
    ("handler", "url"), [("openpdf", DATASHEET), ("openpage", PAGE)]
)
def test_open_buttons_hand_their_link_to_the_default_browser(
    monkeypatch: pytest.MonkeyPatch, handler: str, url: str
) -> None:
    """Each button opens its own link, and nothing is reported when it opens."""
    launch, message, _silenced = _browser(monkeypatch, opened=True)
    dialog = _dialog()

    getattr(dialog, handler)()

    launch.assert_called_once_with(url)
    message.assert_not_called()


def test_a_link_the_system_cannot_open_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed launch is reported once: wx's own error log is silenced."""
    launch, message, silenced = _browser(monkeypatch, opened=False)
    dialog = _dialog()

    dialog.openpdf()

    launch.assert_called_once_with(DATASHEET)
    assert silenced == [True]
    assert not _LogNull.active
    message.assert_called_once()
    assert message.call_args.args == (
        f"Could not open the datasheet for C25804:\n{DATASHEET}",
        "Error",
    )
    assert message.call_args.kwargs["parent"] is dialog


def test_a_link_with_characters_a_url_cannot_carry_is_encoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Datasheet names with "|" or a backslash open; existing escapes stay."""
    launch, _message, _silenced = _browser(monkeypatch, opened=True)
    dialog = _dialog()
    dialog.pdfurl = (
        "https://www.lcsc.com/datasheet/SETsafe--|--SCL%20SD_C2995720.pdf?inline\\"
    )

    dialog.openpdf()

    launch.assert_called_once_with(
        "https://www.lcsc.com/datasheet/SETsafe--%7C--SCL%20SD_C2995720.pdf?inline%5C"
    )


def test_a_missing_link_is_not_launched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A button without a link is disabled; called anyway, it opens nothing."""
    launch, message, _silenced = _browser(monkeypatch, opened=True)
    dialog = _dialog()
    dialog.pageurl = None

    dialog.openpage()

    launch.assert_not_called()
    message.assert_not_called()


def _record(**data: Any) -> dict[str, Any]:
    """Return a successful part fetch carrying ``data``."""
    return {"success": True, "data": {"data": data}}


@pytest.mark.parametrize(
    "placeholder", ["/", "1", "--", "N/A", "file:///C:/Users/me/Downloads/ds.pdf"]
)
def test_a_placeholder_in_the_record_is_no_link(placeholder: str) -> None:
    """Values that aren't web addresses leave the buttons disabled."""
    dialog = _dialog()

    dialog._apply_part_data(
        _record(dataManualUrl=placeholder, lcscGoodsUrl=placeholder), None
    )

    assert (dialog.pdfurl, dialog.pageurl) == (None, None)
    dialog.openpdf_button.Enable.assert_called_once_with(False)
    dialog.openpage_button.Enable.assert_called_once_with(False)


def test_a_link_that_cannot_be_parsed_leaves_the_other_link_working() -> None:
    """A malformed datasheet address is no link; the page link and rows still load."""
    dialog = _dialog()

    dialog._apply_part_data(
        _record(
            componentCode="C25804",
            dataManualUrl="https://[broken/ds.pdf",
            lcscGoodsUrl=PAGE,
        ),
        None,
    )

    assert (dialog.pdfurl, dialog.pageurl) == (None, PAGE)
    dialog.openpdf_button.Enable.assert_called_once_with(False)
    dialog.openpage_button.Enable.assert_called_once_with(True)
    dialog.data_list.AppendItem.assert_any_call(["Component Code", "C25804"])


def test_the_part_record_enables_each_button_that_has_a_link() -> None:
    """A button is enabled only when the record names its link."""
    dialog = _dialog()

    dialog._apply_part_data(_record(dataManualUrl=DATASHEET), None)

    assert dialog.pdfurl == DATASHEET
    assert dialog.pageurl is None
    dialog.openpdf_button.Enable.assert_called_once_with(True)
    dialog.openpage_button.Enable.assert_called_once_with(False)


def test_the_links_are_kept_when_a_row_fails_to_display() -> None:
    """The links are taken before the rows, so a bad row cannot lose them."""
    dialog = _dialog()
    dialog.pdfurl = dialog.pageurl = ""
    dialog.data_list.AppendItem.side_effect = TypeError("bad value")

    with pytest.raises(TypeError):
        dialog._apply_part_data(
            _record(componentCode="C25804", dataManualUrl=DATASHEET, lcscGoodsUrl=PAGE),
            None,
        )

    assert (dialog.pdfurl, dialog.pageurl) == (DATASHEET, PAGE)
    dialog.openpdf_button.Enable.assert_called_once_with(True)
    dialog.openpage_button.Enable.assert_called_once_with(True)
