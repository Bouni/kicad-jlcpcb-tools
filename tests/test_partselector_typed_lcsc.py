"""The part selector offers a searched LCSC number that its results do not show."""

from collections.abc import Callable
import logging
import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as storage, test_window_layout as layout_ui

mainwindow = storage.mainwindow
make_window = storage.make_window
# The selector constructor needs its module's autouse board and callback cleanup,
# which pytest does not discover through a module import.
layout_lifecycle = layout_ui._clear_callbacks

typed_lcsc_offer = layout_ui.partselector.typed_lcsc_offer
UNLISTED = "C46551386"
LISTED = {"type": "Basic", "stock": 22095, "description": "10uF 0603"}


@pytest.mark.parametrize(
    ("keyword", "details", "expected"),
    [
        (
            UNLISTED,
            {},
            (UNLISTED, f"{UNLISTED} isn't in the JLC library. Assign it anyway", {}),
        ),
        (
            " c19702 ",
            LISTED,
            (
                "C19702",
                "C19702 is in the library but not in these results. Assign it",
                LISTED,
            ),
        ),
    ],
)
def test_offer_names_a_searched_number_the_results_do_not_show(
    keyword: str, details: dict[str, Any], expected: tuple[str, str, dict[str, Any]]
) -> None:
    """A substring match on another number does not hide the searched one."""
    lookups: list[str] = []

    def lookup(lcsc: str) -> dict[str, Any]:
        lookups.append(lcsc)
        return details

    assert typed_lcsc_offer(keyword, {"C1970200", "C1970201"}, lookup) == expected
    assert lookups == [expected[0]]


@pytest.mark.parametrize(
    ("keyword", "shown"),
    [
        ("C19702", {"C1970200", "C19702"}),  # The exact number is a result
        ("100nF C0G", set()),  # An ordinary search
        ("C19702 0603", set()),  # A number plus other words
        ("https://www.lcsc.com/product-detail/C19702.html", set()),
        ("", set()),
    ],
)
def test_no_offer_unless_the_keyword_is_one_number_the_results_lack(
    keyword: str, shown: set[str]
) -> None:
    """The offer never appears for a normal search, and never reads the catalog."""
    lookup = MagicMock()

    assert typed_lcsc_offer(keyword, shown, lookup) is None
    lookup.assert_not_called()


def _row(lcsc: str) -> tuple[str, ...]:
    """Build one raw catalog result in the selector's field order."""
    row = {"LCSC Part": lcsc, "Library Type": "Extended", "Stock": "5"}
    return tuple(row.get(field, "") for field in layout_ui.partselector.DB_FIELDS)


def _text_control(
    _parent: Any, _id: int, value: str, *_args: Any, **_kwargs: Any
) -> MagicMock:
    """Keep a text control's value like the native control does."""
    control = MagicMock()
    control.GetValue.return_value = value
    control.ChangeValue.side_effect = lambda new_value: setattr(
        control.GetValue, "return_value", new_value
    )
    return control


@pytest.fixture
def selector(monkeypatch: pytest.MonkeyPatch, make_window: Callable[..., Any]) -> Any:
    """Open the real selector for R1 over real project storage."""
    window = make_window()
    window.window = layout_ui._Dialog()
    window.scale_factor = 2
    window.display_index = 0
    window.save_settings = MagicMock()
    window.library.category_map = {"": []}
    window.library.search = MagicMock(return_value=[_row("C4655138600")])
    partselector = layout_ui.partselector
    monkeypatch.setattr(partselector.wx.TextCtrl, "side_effect", _text_control)
    monkeypatch.setattr(
        partselector.wx.Button, "side_effect", lambda *_args, **_kwargs: MagicMock()
    )
    opened = layout_ui._open_selector(monkeypatch, {}, parent=window, real_search=True)
    opened.update_for({"R1": ""})
    yield opened
    layout_ui._drain_callbacks()


def _search(selector: Any, keyword: str) -> None:
    """Type a keyword and run the real search handler."""
    selector.keyword.ChangeValue(keyword)
    selector.search()


def _click(selector: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Press the offer and return the single event it posted to the main window."""
    queued: list[tuple[Any, Any]] = []
    monkeypatch.setattr(
        layout_ui.partselector.wx,
        "PostEvent",
        lambda target, event: queued.append((target, event)),
        raising=False,
    )
    selector.assign_typed_lcsc()
    assert len(queued) == 1
    target, event = queued[0]
    assert target is selector.parent
    assert not selector
    return event


def test_unlisted_number_is_offered_and_assigned_through_storage(
    selector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One click assigns the number with a blank type and stock."""
    window = selector.parent
    window.library.get_part_details.return_value = {}

    _search(selector, UNLISTED)

    selector.typed_lcsc_button.SetLabel.assert_called_with(
        f"{UNLISTED} isn't in the JLC library. Assign it anyway"
    )
    selector.typed_lcsc_button.Show.assert_called_with(True)
    event = _click(selector, monkeypatch)
    assert (event.lcsc, event.type, event.stock, event.references) == (
        UNLISTED,
        "",
        "",
        ("R1",),
    )
    window.assign_parts(event)
    assert [(row["lcsc"], row["stock"]) for row in storage.project_rows(window)] == [
        (UNLISTED, None)
    ]
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == (
        UNLISTED
    )


def test_listed_number_hidden_from_the_results_carries_catalog_facts(
    selector: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filtered-out part is assigned with the catalog's type and stock."""
    window = selector.parent
    window.library.get_part_details.return_value = dict(LISTED)

    _search(selector, "C19702")

    selector.typed_lcsc_button.SetLabel.assert_called_with(
        "C19702 is in the library but not in these results. Assign it"
    )
    event = _click(selector, monkeypatch)
    assert (event.lcsc, event.type, event.stock) == ("C19702", "Basic", 22095)
    window.assign_parts(event)
    assert [(row["lcsc"], row["stock"]) for row in storage.project_rows(window)] == [
        ("C19702", 22095)
    ]


def test_offer_hides_when_the_results_show_the_number_or_the_catalog_goes(
    selector: Any,
) -> None:
    """A shown number needs no offer, and without a catalog nothing is looked up."""
    window = selector.parent
    window.library.get_part_details.return_value = {}
    button = selector.typed_lcsc_button

    _search(selector, UNLISTED)
    assert selector._typed_lcsc_offer is not None
    window.library.search.return_value = [_row(UNLISTED)]
    _search(selector, UNLISTED)
    assert selector._typed_lcsc_offer is None
    button.Show.assert_called_with(False)

    window.library.search.return_value = [_row("C4655138600")]
    _search(selector, UNLISTED)
    assert selector._typed_lcsc_offer is not None
    window.library.get_part_details.reset_mock()
    window._catalog_ready = False
    _search(selector, UNLISTED)
    assert selector._typed_lcsc_offer is None
    button.Show.assert_called_with(False)
    window.library.get_part_details.assert_not_called()


def test_catalog_read_failure_offers_nothing(
    selector: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed lookup is not mistaken for a number the catalog lacks."""
    selector.parent.library.get_part_details.side_effect = sqlite3.OperationalError(
        "disk I/O error"
    )

    with caplog.at_level(logging.WARNING):
        _search(selector, UNLISTED)

    assert selector._typed_lcsc_offer is None
    assert "disk I/O error" in caplog.text


def test_assigning_an_unlisted_number_leaves_it_unlisted(
    selector: Any, mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next search and the next Enter LCSC still say the catalog lacks it."""
    window = selector.parent
    window.library.get_part_details.return_value = {}
    _search(selector, UNLISTED)
    window.assign_parts(_click(selector, monkeypatch))

    assert typed_lcsc_offer(
        UNLISTED,
        set(),
        lambda lcsc: window._catalog_get_part_details(lcsc, strict=True),
    ) == (UNLISTED, f"{UNLISTED} isn't in the JLC library. Assign it anyway", {})
    question = storage.message_dialog(mainwindow.wx, "NO")
    monkeypatch.setattr(mainwindow.wx, "MessageDialog", question, raising=False)
    assert window.manual_lcsc_details(UNLISTED) is None
    assert len(question.asked) == 1


def test_visible_offer_is_laid_out_again_after_every_search(selector: Any) -> None:
    """The result count changes width each search, so the offer's row is re-laid out."""
    selector.parent.library.get_part_details.return_value = {}
    _search(selector, UNLISTED)
    selector.Layout.reset_mock()

    _search(selector, UNLISTED)

    selector.Layout.assert_called()


@pytest.mark.parametrize("change", ["keyword", "targets"])
def test_failed_search_drops_the_offer(
    selector: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A search that fails cannot leave the last number on offer."""
    window = selector.parent
    window.library.get_part_details.return_value = {}
    _search(selector, UNLISTED)
    assert selector._typed_lcsc_offer is not None
    window.library.search.side_effect = sqlite3.OperationalError("catalog busy")

    with pytest.raises(sqlite3.OperationalError, match="catalog busy"):
        if change == "keyword":
            _search(selector, "C555")
        else:
            selector.update_for({"R9": ""})

    assert selector._typed_lcsc_offer is None
    selector.typed_lcsc_button.Show.assert_called_with(False)
    queued: list[Any] = []
    monkeypatch.setattr(
        layout_ui.partselector.wx,
        "PostEvent",
        lambda *args: queued.append(args),
        raising=False,
    )
    selector.assign_typed_lcsc()
    assert queued == []
