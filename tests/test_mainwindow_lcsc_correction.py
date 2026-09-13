"""Open the Corrections Manager on a part number from the parts list."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.correction_test_support import seed_raw
from tests.part_preferences_test_support import Board, Footprint
from tests.test_corrections_import_export import install_manager_controls
from tests.test_mainwindow_correction_recovery import (
    _population_window,
    runtime as correction_runtime,
)
from tests.test_standard_only_indicator import PartListDataModel, _row

runtime = correction_runtime


def _add_correction_by_lcsc(runtime: SimpleNamespace, *lcsc: str) -> None:
    """Invoke the context-menu handler with one selected part per number given."""
    window = _population_window(runtime)
    selections = [f"item{index}" for index in range(len(lcsc))]
    window.footprint_list = SimpleNamespace(GetSelections=lambda: selections)
    numbers = dict(zip(selections, lcsc))
    window.partlist_data_model.get_lcsc.side_effect = numbers.get
    window.partlist_data_model.get_reference.side_effect = lambda item: (
        f"U{selections.index(item) + 1}"
    )
    event_id = runtime.mainwindow.ID_CONTEXT_MENU_ADD_ROT_BY_LCSC
    window.add_correction(SimpleNamespace(GetId=lambda: event_id))


def test_add_correction_by_lcsc_opens_the_manager_in_part_mode(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dialog arrives prefilled with the part's number and the box ticked."""
    install_manager_controls(runtime.modules, runtime.library, monkeypatch)
    opened = []
    monkeypatch.setattr(
        runtime.wx.Dialog,
        "ShowModal",
        lambda dialog: opened.append(dialog) or runtime.wx.ID_OK,
        raising=False,
    )

    _add_correction_by_lcsc(runtime, "c12345")

    (dialog,) = opened
    assert dialog.regex.GetValue() == "c12345"
    assert dialog.lcsc_mode.GetValue() is True
    runtime.wx.MessageBox.assert_not_called()


def test_add_correction_by_lcsc_warns_once_for_unassigned_parts(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parts without a number cannot have a part rule; one warning names them all."""
    install_manager_controls(runtime.modules, runtime.library, monkeypatch)
    opened = []
    monkeypatch.setattr(
        runtime.wx.Dialog,
        "ShowModal",
        lambda dialog: opened.append(dialog) or runtime.wx.ID_OK,
        raising=False,
    )

    _add_correction_by_lcsc(runtime, "", "C12345", "")

    assert [dialog.regex.GetValue() for dialog in opened] == ["C12345"]
    runtime.wx.MessageBox.assert_called_once()
    assert "U1, U3" in runtime.wx.MessageBox.call_args.args[0]
    assert "No LCSC number" in str(runtime.wx.MessageBox.call_args)


def _window_with_part(runtime: SimpleNamespace, lcsc: str) -> tuple[Any, dict]:
    """Bind the real handlers to one part whose store row follows assignments."""
    part = {
        "reference": "U1",
        "value": "Device",
        "footprint": "SOT-23-3",
        "lcsc": lcsc,
        "exclude_from_bom": 0,
        "exclude_from_pos": 0,
    }
    window = _population_window(runtime)
    board = Board([Footprint("U1", value="Device", footprint="SOT-23-3", lcsc=lcsc)])
    window.pcbnew = SimpleNamespace(GetBoard=lambda: board)
    # The refreshed cell, not the remembered preference, is what is under test.
    window.settings = {"part_preferences": {"remember_lcsc_assignments": False}}
    window.store.get_part = lambda _reference: part
    window.store.set_lcsc_assignments = lambda assignments: [
        part.__setitem__("lcsc", number) for _reference, number, _stock in assignments
    ]
    window.library.get_part_details = lambda _lcsc: {"type": "Basic", "stock": 1}
    window.start_assembly_enrichment = MagicMock()
    window.recompute_bom_estimate = MagicMock()
    window.logger = MagicMock()
    window.footprint_list = SimpleNamespace(GetSelections=lambda: ["item"])
    window.partlist_data_model.get_reference.return_value = "U1"
    window.partlist_data_model.get_footprint.return_value = "SOT-23-3"
    window.partlist_data_model.get_value.return_value = "Device"
    return window, part


def _seed_rules(runtime: SimpleNamespace) -> None:
    """Store a family rule and a part rule that overrides it."""
    runtime.library.apply_corrections([("SOT-23-3", 180, (1, 1))])
    runtime.library.insert_lcsc_correction_data("C12345", 90, (0.5, -0.5))


PART_RULE = "90°, 0.5/-0.5 (lcsc)"
FAMILY_RULE = "180°, 1.0/1.0 (fpt)"


def test_assigning_a_part_refreshes_its_correction_cell(
    runtime: SimpleNamespace,
) -> None:
    """The rule shown follows the part number the part selector assigns."""
    _seed_rules(runtime)
    window, part = _window_with_part(runtime, "")

    window.assign_parts(
        SimpleNamespace(lcsc="C12345", references=["U1"], type="Basic", stock=1)
    )

    assert part["lcsc"] == "C12345"
    window.partlist_data_model.set_correction.assert_called_once_with("U1", PART_RULE)
    window.partlist_data_model.AddEntry.assert_not_called()


def test_removing_a_part_number_refreshes_its_correction_cell(
    runtime: SimpleNamespace,
) -> None:
    """Without its number the part falls back to the family rule, visibly."""
    _seed_rules(runtime)
    window, part = _window_with_part(runtime, "C12345")

    window.remove_lcsc_number()

    assert part["lcsc"] == ""
    window.partlist_data_model.set_correction.assert_called_once_with("U1", FAMILY_RULE)


def test_pasting_a_part_number_refreshes_its_correction_cell(
    runtime: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pasted number reaches the store sanitized and still finds its rule."""
    _seed_rules(runtime)
    window, part = _window_with_part(runtime, "")
    monkeypatch.setattr(
        runtime.wx,
        "TextDataObject",
        lambda: SimpleNamespace(GetText=lambda: "c12345"),
        raising=False,
    )
    monkeypatch.setattr(
        runtime.wx,
        "TheClipboard",
        SimpleNamespace(
            Open=lambda: True, GetData=lambda _data: True, Close=lambda: None
        ),
        raising=False,
    )

    window.paste_part_lcsc()

    assert part["lcsc"] == "C12345"
    window.partlist_data_model.set_correction.assert_called_once_with("U1", PART_RULE)


def test_an_applied_part_preference_refreshes_its_correction_cell(
    runtime: SimpleNamespace,
) -> None:
    """Apply part preferences changes the number, so it changes the rule."""
    _seed_rules(runtime)
    window, part = _window_with_part(runtime, "")
    window.library.get_part_preference = lambda _footprint, _value: "C12345"

    window.apply_selected_part_preferences()

    assert part["lcsc"] == "C12345"
    window.partlist_data_model.set_correction.assert_called_once_with("U1", PART_RULE)


def test_refresh_shows_unresolved_and_the_status_when_storage_needs_repair(
    runtime: SimpleNamespace,
) -> None:
    """A refreshed cell never shows a partial correction set as valid."""
    _seed_rules(runtime)
    seed_raw(runtime.library, [("R1", "47u", 0, 0)])
    window, _part = _window_with_part(runtime, "")

    window.assign_parts(
        SimpleNamespace(lcsc="C12345", references=["U1"], type="Basic", stock=1)
    )

    window.partlist_data_model.set_correction.assert_called_once_with(
        "U1", "Unresolved"
    )
    assert window.correction_status.IsShown()


def test_set_correction_changes_only_that_rows_cell() -> None:
    """The data model updates one Correction cell in place, or nothing."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.AddEntry(_row("R2"))
    model.ItemChanged = MagicMock()
    column = model.columns["ROT_COL"]

    model.set_correction("R2", PART_RULE)
    model.set_correction("R9", FAMILY_RULE)

    assert [row[column] for row in model.data] == ["0", PART_RULE]
    model.ItemChanged.assert_called_once()
