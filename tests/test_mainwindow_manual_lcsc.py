"""Enter LCSC assigns a typed number, including one the catalog does not list."""

from collections.abc import Callable
import sqlite3
from typing import Any
from unittest.mock import MagicMock

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import (
    Footprint,
    act,
    entry_dialog,
    message_dialog,
    project_rows,
)

mainwindow = support.mainwindow
make_window = support.make_window

UNLISTED = "C46551386"


def _enter(
    window: Any,
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    code: str = UNLISTED,
    *,
    accept: bool = True,
    answer: str = "YES",
) -> tuple[Any, Any]:
    """Run the real handler with the prompt typed and the question answered."""
    dialog = entry_dialog(mainwindow.wx, code, accept=accept)
    question = message_dialog(mainwindow.wx, answer)
    monkeypatch.setattr(mainwindow, "LcscEntryDialog", dialog)
    monkeypatch.setattr(mainwindow.wx, "MessageDialog", question, raising=False)
    window.enter_part_lcsc()
    return dialog, question


def _unchanged(window: Any) -> None:
    """Assert the board, project and table still hold the starting C100."""
    assert [row["lcsc"] for row in project_rows(window)] == ["C100", "C100"]
    assert all(
        fp.field.text == "C100" for fp in window.pcbnew.GetBoard().GetFootprints()
    )
    assert all(row["lcsc"] == "C100" for row in window.test_rows.values())
    assert window.library.get_all_part_preferences() == []
    window.start_assembly_enrichment.assert_not_called()


def test_listed_number_is_assigned_with_catalog_facts_without_asking(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A number the catalog lists behaves like a picked part."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window.library.get_part_details.return_value = {
        "type": "Extended",
        "stock": 5,
        "description": "10uF 0603",
    }

    dialog, question = _enter(window, mainwindow, monkeypatch, "C19702")

    assert dialog.opened == [(("R1", "R2"), "C100")]
    assert question.asked == []
    window.library.get_part_details.assert_called_once_with("C19702")
    assert [(row["lcsc"], row["stock"]) for row in project_rows(window)] == [
        ("C19702", 5),
        ("C19702", 5),
    ]
    assert all(
        fp.field.text == "C19702" for fp in window.pcbnew.GetBoard().GetFootprints()
    )
    assert [(row["type"], row["stock"]) for row in window.test_rows.values()] == [
        ("Extended", 5),
        ("Extended", 5),
    ]
    assert window.library.get_all_part_preferences() == [["R_0603", "10k", "C19702"]]
    window.start_assembly_enrichment.assert_called_once_with(["R1", "R2"])


def test_unlisted_number_is_asked_about_once_then_assigned(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yes assigns the number with a blank type and stock, and remembers it."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window.library.get_part_details.return_value = {}

    _dialog, question = _enter(window, mainwindow, monkeypatch)

    assert len(question.asked) == 1
    assert f"{UNLISTED} isn't in the downloaded JLC parts library" in question.asked[0]
    assert "pre-order or global sourcing" in question.asked[0]
    assert [(row["lcsc"], row["stock"]) for row in project_rows(window)] == [
        (UNLISTED, None),
        (UNLISTED, None),
    ]
    assert all(
        fp.field.text == UNLISTED for fp in window.pcbnew.GetBoard().GetFootprints()
    )
    assert [(row["type"], row["stock"]) for row in window.test_rows.values()] == [
        ("", ""),
        ("", ""),
    ]
    assert window.library.get_all_part_preferences() == [["R_0603", "10k", UNLISTED]]


def test_declining_the_question_changes_nothing(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No leaves every representation of the selection as it was."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window.library.get_part_details.return_value = {}

    _dialog, question = _enter(window, mainwindow, monkeypatch, answer="NO")

    assert len(question.asked) == 1
    _unchanged(window)


@pytest.mark.parametrize("catalog", ["not downloaded", "downloading"])
def test_without_a_readable_catalog_the_number_is_assigned_without_reading_or_asking(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    catalog: str,
) -> None:
    """No catalog, or one being rewritten in place, is neither read nor asked about."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    if catalog == "not downloaded":
        window._catalog_ready = False
    else:
        window.library.is_download_running.return_value = True

    _dialog, question = _enter(window, mainwindow, monkeypatch)

    window.library.get_part_details.assert_not_called()
    assert question.asked == []
    assert [row["lcsc"] for row in project_rows(window)] == [UNLISTED, UNLISTED]
    assert all(
        fp.field.text == UNLISTED for fp in window.pcbnew.GetBoard().GetFootprints()
    )
    window.logger.warning.assert_not_called()


@pytest.mark.parametrize("action", ["picker", "paste"])
def test_other_assignment_paths_still_require_the_catalog(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    """Only a typed number bypasses the catalog check."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window._catalog_ready = False

    act(action, window, mainwindow, monkeypatch, "C200")

    _unchanged(window)
    assert "catalog is unavailable" in str(window.logger.warning.call_args)


def test_catalog_read_failure_assigns_nothing(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup that fails is not mistaken for an unlisted part."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])
    window.library.get_part_details.side_effect = sqlite3.OperationalError(
        "disk I/O error"
    )

    _dialog, question = _enter(window, mainwindow, monkeypatch)

    assert question.asked == []
    _unchanged(window)
    warning = str(window.logger.warning.call_args)
    assert UNLISTED in warning and "disk I/O error" in warning


def test_cancelled_prompt_changes_nothing(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancel reads nothing and asks nothing."""
    window = make_window(footprints=[Footprint("R1"), Footprint("R2")])

    dialog, question = _enter(window, mainwindow, monkeypatch, accept=False)

    assert len(dialog.opened) == 1
    window.library.get_part_details.assert_not_called()
    assert question.asked == []
    _unchanged(window)


@pytest.mark.parametrize(
    ("numbers", "initial"),
    [(("C100", "C100"), "C100"), (("C100", "C200"), ""), (("", ""), "")],
)
def test_prompt_opens_on_the_number_the_selection_shares(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    numbers: tuple[str, str],
    initial: str,
) -> None:
    """One shared number is offered for editing; mixed numbers start empty."""
    window = make_window(
        footprints=[
            Footprint("R1", lcsc=numbers[0]),
            Footprint("R2", lcsc=numbers[1]),
        ]
    )

    dialog, _question = _enter(window, mainwindow, monkeypatch, accept=False)

    assert dialog.opened == [(("R1", "R2"), initial)]


@pytest.mark.parametrize("missing", ["selection", "storage"])
def test_prompt_is_not_opened_without_a_selection_or_storage(
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    """Nothing to assign to, or nowhere to keep it, opens no prompt."""
    window = make_window()
    if missing == "selection":
        window.footprint_list.GetSelections.return_value = []
    else:
        window.store = None

    dialog, _question = _enter(window, mainwindow, monkeypatch)

    assert dialog.opened == []
    if missing == "storage":
        assert "project storage is unavailable" in str(window.logger.warning.call_args)


def test_remembering_follows_the_setting(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With remembering off the number is assigned but not saved as a preference."""
    window = make_window(
        settings={"part_preferences": {"remember_lcsc_assignments": False}}
    )
    window.library.get_part_details.return_value = {}

    _enter(window, mainwindow, monkeypatch)

    assert window.store.get_part("R1")["lcsc"] == UNLISTED
    assert window.library.get_all_part_preferences() == []


def test_variant_view_handles_the_action(
    make_window: Callable[..., Any], mainwindow: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With variants the controller owns targets, prompting and editing."""
    window = make_window()
    controller = window._variant_controller = MagicMock()

    dialog, _question = _enter(window, mainwindow, monkeypatch)

    controller.dispatch_action.assert_called_once_with(
        "enter_lcsc", controller.view.selected_target
    )
    assert dialog.opened == []
