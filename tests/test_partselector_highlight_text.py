"""The part selector highlights what its search actually looked for."""

from itertools import count
from types import SimpleNamespace
from unittest.mock import Mock

from .wx_harness import load, mainwindow_stubs, wx_stubs

_PACKAGE = "partselector_highlight_text_tests"
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


def _selector(text: str, settings: dict) -> object:
    """Return a part selector whose keyword box holds ``text``."""
    selector = object.__new__(partselector.PartSelectorDialog)
    selector.parent = SimpleNamespace(settings=settings)
    selector.keyword = Mock()
    selector.keyword.GetValue.return_value = text
    return selector


def test_micro_and_ohm_signs_are_highlighted_as_the_catalog_writes_them():
    """A 10µF search finds 10uF parts, so the highlight looks for 10uF too."""
    selector = _selector("10\u00b5F 5\u2126 0805", {})
    assert selector.get_highlight_text() == "10uF 5\u03a9 0805"


def test_highlighting_can_still_be_switched_off():
    """The setting that turns match highlighting off still wins."""
    selector = _selector("10\u00b5F", {"highlighting": {"matches": False}})
    assert selector.get_highlight_text() == ""
