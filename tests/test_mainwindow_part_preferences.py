"""Part preference lifecycle with real project and shared SQLite persistence."""

from collections.abc import Callable
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import call

import pytest

from . import part_preferences_test_support as support
from .part_preferences_test_support import Footprint, act, info_messages

mainwindow = support.mainwindow
make_window = support.make_window


@pytest.mark.parametrize("action", ["picker", "paste"])
def test_remember_part_preferences_can_be_disabled_without_blocking_assignment(
    action: str,
    make_window: Callable[..., Any],
    mainwindow: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling learning must leave explicit assignments fully functional."""
    window = make_window(
        settings={"part_preferences": {"remember_lcsc_assignments": False}}
    )

    act(action, window, mainwindow, monkeypatch, "C200")

    assert window.store.get_part("R1")["lcsc"] == "C200"
    window.library.save_part_preferences.assert_not_called()


def test_part_preferences_fill_only_eligible_blank_parts_before_initial_population(
    make_window: Callable[..., Any],
) -> None:
    """Opening must respect existing assignments, exclusions and complete part preference keys."""
    footprints = [
        Footprint("R1", lcsc=""),
        Footprint("R2", lcsc=""),
        Footprint("R_ASSIGNED", lcsc="C300"),
        Footprint("R_DNP", lcsc="", dnp=True),
        Footprint("R_BOM", lcsc="", bom=True),
        Footprint("R_POS", lcsc="", pos=True),
        Footprint("R_NO_VALUE", lcsc="", value=""),
        Footprint("R_NO_FOOTPRINT", lcsc="", footprint=""),
        Footprint("R_NO_PREFERENCE", lcsc="", value="22k"),
    ]
    window = make_window(
        footprints=footprints,
        part_preferences={
            ("R_0603", "10k"): "C200",
            ("R_0603", ""): "C200",
            ("", "10k"): "C200",
        },
    )
    # The opening scan must read every store row, even when current UI rows are filtered.
    window.test_rows.clear()

    window.init_store()

    actual = {part["reference"]: part["lcsc"] for part in window.store.read_all()}
    assert actual == {
        fp.reference: (
            "C200"
            if fp.reference in {"R1", "R2"}
            else "C300"
            if fp.reference == "R_ASSIGNED"
            else ""
        )
        for fp in footprints
    }
    assert window.test_rows["R1"]["lcsc"] == "C200"
    assert window.test_rows["R2"]["lcsc"] == "C200"
    window.library.save_part_preferences.assert_not_called()

    window.start_assembly_enrichment.assert_called_once_with()
    window.recompute_bom_estimate.assert_called_once_with()


def test_part_preferences_respect_existing_project_assignment(
    make_window: Callable[..., Any],
) -> None:
    """A saved project choice must take precedence over shared part preferences."""
    window = make_window(
        footprints=[Footprint(lcsc="")], part_preferences={("R_0603", "10k"): "C200"}
    )
    window.store.set_lcsc("R1", "C300")

    window.init_store()

    assert window.store.get_part("R1")["lcsc"] == "C300"
    window.library.get_part_preference.assert_not_called()


def test_part_preferences_wait_for_initialized_library_and_apply_once_per_open(
    make_window: Callable[..., Any], mainwindow: Any
) -> None:
    """Clearing remains effective until the next window despite refresh and reinitialization."""
    window = make_window(
        footprints=[Footprint(lcsc="")], part_preferences={("R_0603", "10k"): "C200"}
    )
    window.library.state = mainwindow.LibraryState.UPDATE_NEEDED
    window.init_store()
    assert window.store.get_part("R1")["lcsc"] == ""
    window.library.get_part_preference.assert_not_called()

    window.library.state = mainwindow.LibraryState.INITIALIZED
    window.init_store()
    assert window.store.get_part("R1")["lcsc"] == "C200"
    window.remove_lcsc_number()
    mainwindow.JLCPCBTools.populate_footprint_list(window)
    window.init_store()

    assert window.store.get_part("R1")["lcsc"] == ""
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == ""
    window.library.get_part_preference.assert_called_once_with("R_0603", "10k")
    window.library.save_part_preferences.assert_not_called()

    assert window.library.get_all_part_preferences() == [["R_0603", "10k", "C200"]]

    messages = info_messages(window)
    assert len(messages) == 1
    assert messages[0].startswith("Filled 1 empty LCSC assignment(s)")
    assert "Parts preferences fill in empty LCSC assignments" in messages[0]
    assert "Settings > Part preferences" in messages[0]

    reopened = make_window(board=window.pcbnew.GetBoard())
    reopened.init_store()
    assert reopened.store.get_part("R1")["lcsc"] == "C200"


def test_manual_part_preference_actions_work_with_automatic_settings_disabled(
    make_window: Callable[..., Any],
) -> None:
    """Manual part preference saving and application work with automation disabled."""
    window = make_window(
        settings={
            "part_preferences": {
                "remember_lcsc_assignments": False,
                "fill_empty_lcsc_assignments_on_open": False,
            }
        }
    )
    window.save_selected_part_preferences()
    window.library.save_part_preferences.assert_called_once_with(
        [("R_0603", "10k", "C100")]
    )
    window.remove_lcsc_number()
    window.init_store()
    assert window.store.get_part("R1")["lcsc"] == ""
    window.library.get_part_preference.assert_not_called()

    window.apply_selected_part_preferences()

    assert window.store.get_part("R1")["lcsc"] == "C100"
    assert window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == "C100"
    assert window.library.save_part_preferences.call_args_list == [
        call([("R_0603", "10k", "C100")])
    ]


def test_part_preferences_skip_footprint_deleted_after_board_reconciliation(
    make_window: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deletion between board sync and part preference lookup must not interrupt opening."""
    window = make_window(
        footprints=[Footprint(lcsc=""), Footprint("R2", lcsc="")],
        part_preferences={("R_0603", "10k"): "C200"},
    )
    board = window.pcbnew.GetBoard()
    monkeypatch.setattr(
        board,
        "FindFootprintByReference",
        lambda ref: (board.footprints.get(ref) if ref != "R1" else None),
    )

    window.init_store()

    assert window.store.get_part("R1")["lcsc"] == ""
    assert window.store.get_part("R2")["lcsc"] == "C200"


def test_part_preference_database_failure_still_opens_project(
    make_window: Callable[..., Any],
) -> None:
    """Part preference database read failures must still populate the project window."""
    window = make_window(footprints=[Footprint(lcsc="")])
    window.library.get_part_preference.side_effect = sqlite3.OperationalError(
        "unavailable"
    )

    window.init_store()

    assert window.test_rows["R1"]["lcsc"] == ""
    window.start_assembly_enrichment.assert_called_once_with()
    assert window.logger.warning.called


@pytest.mark.parametrize("key", [{"value": ""}, {"footprint": ""}])
def test_assignment_with_incomplete_part_preference_key_still_updates_project(
    key: dict[str, str], make_window: Callable[..., Any]
) -> None:
    """An incomplete shared key must not prevent local assignment."""
    window = make_window(footprints=[Footprint(**key)])

    window.assign_parts(
        SimpleNamespace(references=["R1"], lcsc="C200", type="Basic", stock=27)
    )

    assert window.store.get_part("R1")["lcsc"] == "C200"
    window.library.save_part_preferences.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"LCSC": "Z123"},
        {"LCSC": "A456"},
        {"LCSC": "not assigned yet"},
        {"LCSC": " \t "},
        {"LCSC": "", "JLCPCB": "Z123"},
    ],
)
def test_opening_preserves_every_occupied_assignment_field(
    make_window: Callable[..., Any], fields: dict[str, str]
) -> None:
    """Automatic preferences must not treat rejected identifier text as empty."""
    footprint = Footprint(fields=fields)
    window = make_window(
        footprints=[footprint], part_preferences={("R_0603", "10k"): "C999"}
    )

    window.init_store()
    window.init_store()
    messages = info_messages(window)
    assert len(messages) == 1
    assert "R1" in messages[0]
    occupied_name, occupied_text = next(
        (name, text) for name, text in fields.items() if text
    )
    assert repr(occupied_name) in messages[0]
    assert repr(occupied_text) in messages[0]
    window.library.save_part_preferences.assert_not_called()
    reopened = make_window(board=window.pcbnew.GetBoard())
    reopened.init_store()

    assert {name: field.text for name, field in footprint.fields.items()} == fields


@pytest.mark.parametrize("text", ["c123", "C123 "])
def test_opening_reads_an_untidily_typed_assignment_as_the_part_it_names(
    make_window: Callable[..., Any], text: str
) -> None:
    """A field needing only normalisation names a part, so it is not empty.

    These two spellings used to be rejected identifiers, and the fill only
    left them alone because the occupied-field check caught them. Now the
    reader normalises before testing (issue #773), so the part is assigned to
    C123 outright, the fill skips it as it skips any assigned part, and
    nothing is logged or written back over what the user typed.
    """
    footprint = Footprint(fields={"LCSC": text})
    window = make_window(
        footprints=[footprint], part_preferences={("R_0603", "10k"): "C999"}
    )

    window.init_store()

    assert window.store.get_part("R1")["lcsc"] == "C123"
    assert info_messages(window) == []
    window.library.get_part_preference.assert_not_called()
    assert footprint.fields["LCSC"].text == text


@pytest.mark.parametrize("action", ["open", "apply"])
@pytest.mark.parametrize(
    "saved, expected",
    [(" C200 ", "C200"), ("c200", "C200"), ("Z200", ""), ("C200junk", ""), ("", "")],
)
def test_saved_preferences_are_validated_before_application(
    make_window: Callable[..., Any],
    caplog: pytest.LogCaptureFixture,
    action: str,
    saved: str,
    expected: str,
) -> None:
    """Installed invalid rows remain stored but never propagate into assignments."""
    window = make_window(
        footprints=[Footprint(lcsc="")], part_preferences={("R_0603", "10k"): saved}
    )
    if action == "open":
        window.init_store()
    else:
        window.apply_selected_part_preferences()
    assert window.store.get_part("R1")["lcsc"] == expected
    assert window.test_rows["R1"]["lcsc"] == expected
    assert (
        window.pcbnew.GetBoard().FindFootprintByReference("R1").field.text == expected
    )
    assert window.library.get_all_part_preferences() == [["R_0603", "10k", saved]]
    if not expected:
        assert "invalid" in caplog.text.lower()


@pytest.mark.parametrize("preference", [None, "Z999", "C999"])
def test_supplier_metadata_skip_is_reported_only_for_a_usable_preference(
    make_window: Callable[..., Any],
    preference: Any,
) -> None:
    """The conservative raw-field guard explains actual blocked autofill choices."""
    window = make_window(
        footprints=[Footprint(fields={"JLC Rotation": "90"})],
        part_preferences={("R_0603", "10k"): preference}
        if preference is not None
        else {},
    )
    window.init_store()
    window.init_store()
    messages = info_messages(window)
    assert len(messages) == (1 if preference == "C999" else 0)
    if messages:
        assert (
            "R1" in messages[0]
            and "JLC Rotation" in messages[0]
            and "90" in messages[0]
        )
    assert window.store.get_part("R1")["lcsc"] == ""
