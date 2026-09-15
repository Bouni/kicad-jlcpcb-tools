"""Tests for the computed Standard-only indicator column."""

import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

_ROOT = Path(__file__).parent.parent
_PACKAGE = "standard_indicator_plugin"


class _DataViewModel:
    ObjectToItem = ItemToObject = staticmethod(lambda value: value)
    HasValue = lambda self, _item, _column: True
    ItemAdded = ItemChanged = ValueChanged = Cleared = lambda self, *_args: None


def _module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_datamodel() -> types.ModuleType:
    dataview = _module(
        "wx.dataview",
        PyDataViewModel=_DataViewModel,
        DataViewIconText=lambda *values: values,
        NullDataViewItem=None,
    )
    wx = _module("wx", Colour=lambda *rgb: rgb, dataview=dataview)
    package = _module(_PACKAGE, __path__=[str(_ROOT)])
    modules = {
        "wx": wx,
        "wx.dataview": dataview,
        _PACKAGE: package,
        f"{_PACKAGE}.dataview_highlight": MagicMock(),
        f"{_PACKAGE}.helpers": MagicMock(),
        f"{_PACKAGE}.partselector_columns": MagicMock(),
    }
    name = f"{_PACKAGE}.datamodel"
    spec = importlib.util.spec_from_file_location(name, _ROOT / "datamodel.py")
    module = importlib.util.module_from_spec(spec)
    modules[name] = module
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


datamodel = _load_datamodel()
PartListDataModel = datamodel.PartListDataModel


def _row(reference: str, lcsc: str = "C1") -> list[Any]:
    return [reference, "10k", "R_0603", lcsc, "Basic", "100"] + ["0"] * 8


def test_assigned_unknown_classification_has_a_visible_read_only_indicator() -> None:
    """Unknown assigned parts must be distinguishable without Enrichment column."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    item = model.ObjectToItem(model.data[0])
    column = model.columns["STANDARD_ONLY_COL"]
    assert model.GetValue(item, column) == "?"
    assert model.GetColumnType(column) == "string"
    assert model.HasValue(item, column) is True


@pytest.mark.parametrize(
    "lcsc, classification, pending, glyph, label",
    [
        ("C1", 2, False, "✓", "Standard Only"),
        ("C1", 1, False, "—", "Economic Only"),
        ("C1", 0, False, "—", "Economic and Standard"),
        ("C1", "2", False, "✓", "Standard Only"),
        ("C1", None, True, "◷", "unavailable"),
        ("C1", None, False, "?", "unavailable"),
        ("C1", True, False, "?", "unavailable"),
        ("C1", "invalid", False, "?", "unavailable"),
        ("C1", 0.5, False, "?", "unavailable"),
        ("C1", 7, False, "?", "unavailable"),
        ("", None, False, "", None),
        ("", 2, True, "", None),
    ],
)
def test_indicator_is_read_only_unstyled_and_reports_available_classification(
    lcsc: str, classification: object, pending: bool, glyph: str, label: object
) -> None:
    """Show assigned assembly states without editable toggles or row styling."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1", lcsc))
    item = model.ObjectToItem(model.data[0])
    column = model.columns["STANDARD_ONLY_COL"]

    model.set_assembly_metadata(
        "R1",
        {"lcsc": lcsc, "component_product_type": classification},
        pending=pending,
    )

    assert (model.GetColumnCount(), model.GetColumnType(column)) == (16, "string")
    assert all(len(row) == 15 for row in model.data)
    assert model.HasValue(item, column) is bool(lcsc)
    assert model.GetValue(item, column) == glyph
    assert model.SetValue("✓", item, column) is False
    tooltip = model.get_assembly_tooltip(item)
    if lcsc:
        assert f"Assembly classification: {label}" in tooltip
        assert "Assembly process: unavailable" in tooltip
        assert ("Retrieving assembly information." in tooltip) is pending
    else:
        assert tooltip == "No assigned LCSC part."

    attr = MagicMock()
    assert model.GetAttr(item, model.columns["REF_COL"], attr) is False
    assert model.GetAttr(item, column, attr) is False
    attr.SetColour.assert_not_called()
    attr.SetBold.assert_not_called()


@pytest.mark.parametrize("action", ["reassign", "remove", "reset"])
def test_indicator_state_clears_on_lcsc_reassignment_and_row_reset(
    action: str,
) -> None:
    """Discard stale classification state when rows or assignments change."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.set_standard_only_refs({"R1"})
    model.set_assembly_metadata(
        "R1", {"lcsc": "C1", "component_product_type": 2, "assembly_process": "SMT"}
    )
    item = model.ObjectToItem(model.data[0])
    column = model.columns["STANDARD_ONLY_COL"]

    if action == "reassign":
        model.set_lcsc("R1", "C2", "Basic", "50", "new params")
    elif action == "remove":
        model.remove_lcsc_number(item)
    else:
        model.RemoveAll()
        model.AddEntry(_row("R1", "C2"))
        item = model.ObjectToItem(model.data[0])

    assert model.GetValue(item, column) == ("" if action == "remove" else "?")
    assert "Standard Only" not in model.get_assembly_tooltip(item)
    assert "SMT" not in model.get_assembly_tooltip(item)
    assert model.standard_only_refs == set()


def test_estimator_standard_refs_remain_separate_from_classification() -> None:
    """A filtered estimate must not erase known data or imply Economic eligibility."""
    model = PartListDataModel(scale_factor=1.0)
    for reference in ("R1", "R2", "R3"):
        model.AddEntry(_row(reference))
    model.set_assembly_metadata("R1", {"component_product_type": 2})
    model.set_assembly_metadata("R2", {"component_product_type": 1})

    model.set_standard_only_refs({"R3"})
    assert [
        model.GetValue(row, model.columns["STANDARD_ONLY_COL"]) for row in model.data
    ] == ["✓", "—", "?"]
    assert model.is_standard_only(model.data[2]) is True
    model.set_standard_only_refs(set())
    assert model.GetValue(model.data[0], model.columns["STANDARD_ONLY_COL"]) == "✓"


def test_indicator_notifies_only_rows_whose_state_changed() -> None:
    """Use row notifications because computed model indices exceed native columns."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.AddEntry(_row("R2"))
    model.ValueChanged = MagicMock()
    model.ItemChanged = MagicMock()

    model.set_standard_only_refs({"R1"})
    assert model.ItemChanged.call_args_list == [call(model.data[0])]

    model.ItemChanged.reset_mock()
    model.set_standard_only_refs({"R1"})
    model.ItemChanged.assert_not_called()

    model.set_standard_only_refs({"R2"})
    assert model.ItemChanged.call_args_list == [
        call(model.data[0]),
        call(model.data[1]),
    ]
    model.ValueChanged.assert_not_called()


def test_indicator_tooltip_copy_is_exact() -> None:
    """Keep hover guidance terse and stable."""
    assert datamodel.STANDARD_ONLY_TOOLTIP == (
        "Part cannot be assembled in economy mode, standard must be used"
    )


@pytest.mark.parametrize("classification, glyph", [(0, "—"), (1, "—"), (2, "✓")])
def test_known_classification_remains_during_incomplete_process_lookup(
    classification: int, glyph: str
) -> None:
    """Pending and empty process responses must preserve cached classification."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    item = model.ObjectToItem(model.data[0])
    part = {"lcsc": "C1", "component_product_type": classification}
    for pending in (False, True, False):
        model.set_assembly_metadata("R1", part, pending=pending)
        assert model.GetValue(item, model.columns["STANDARD_ONLY_COL"]) == glyph
        tooltip = model.get_assembly_tooltip(item)
        assert ("Retrieving assembly information." in tooltip) is pending
        assert "Assembly process: unavailable" in tooltip


def test_pending_completes_per_row_without_waiting_for_other_rows() -> None:
    """Classify individual completions while other requests remain pending."""
    model = PartListDataModel(scale_factor=1.0)
    for reference in ("R1", "R2"):
        model.AddEntry(_row(reference))
        model.set_assembly_metadata(reference, {}, pending=True)
    model.set_assembly_metadata(
        "R1", {"component_product_type": 2, "assembly_process": "SMT"}
    )

    first, second = model.data
    column = model.columns["STANDARD_ONLY_COL"]
    assert (model.GetValue(first, column), model.GetValue(second, column)) == ("✓", "◷")
    assert "Assembly process: SMT" in model.get_assembly_tooltip(first)
    assert "Retrieving" not in model.get_assembly_tooltip(first)
    model.set_assembly_metadata("R2", {})
    assert model.GetValue(second, column) == "?"


def test_old_assignment_metadata_cannot_repopulate_a_reassigned_row() -> None:
    """Ignore a result belonging to the previous LCSC assignment."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.set_lcsc("R1", "C2", "Basic", "50", "")

    model.set_assembly_metadata(
        "R1", {"lcsc": "C1", "component_product_type": 2, "assembly_process": "SMT"}
    )

    assert model.GetValue(model.data[0], model.columns["STANDARD_ONLY_COL"]) == "?"
    assert "SMT" not in model.get_assembly_tooltip(model.data[0])


def test_metadata_updates_notify_changed_row_without_duplicate_notifications() -> None:
    """Update native cells when status or tooltip changes, even for stable glyphs."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.AddEntry(_row("R2"))
    model.ValueChanged = MagicMock()
    model.ItemChanged = MagicMock()
    part = {"lcsc": " c1 ", "component_product_type": 2}
    model.set_assembly_metadata("R1", part)
    model.set_assembly_metadata("R1", part)
    assert model.ItemChanged.call_args_list == [call(model.data[0])]
    model.ItemChanged.reset_mock()
    model.set_assembly_metadata("R1", part, pending=True)
    assert model.ItemChanged.call_args_list == [call(model.data[0])]
    model.ValueChanged.assert_not_called()


@pytest.mark.parametrize(
    "classification, pending, expected_status",
    [(2, False, "Done"), (None, True, "Pending"), (None, False, "Class missing")],
)
def test_projected_metadata_updates_hidden_status_with_the_visible_indicator(
    classification: object, pending: bool, expected_status: str
) -> None:
    """Completion must clear obsolete Pending text in the retained model slot."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.set_enrichment_status("R1", "Pending")
    model.set_assembly_metadata(
        "R1", {"component_product_type": classification}, pending=pending
    )
    assert model.data[0][model.columns["ENRICH_COL"]] == expected_status


def test_legacy_enrichment_status_preserves_metadata_and_controls_pending() -> None:
    """Existing request scheduling can mark loading without querying cached data."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.set_assembly_metadata("R1", {"component_product_type": 2})
    for status in ("Pending", "Done", "Pending", "", "Classification missing"):
        model.set_enrichment_status("R1", status)
        item = model.ObjectToItem(model.data[0])
        assert model.GetValue(item, model.columns["STANDARD_ONLY_COL"]) == "✓"
        assert (
            "Retrieving assembly information." in model.get_assembly_tooltip(item)
        ) is (status == "Pending")


def test_unknown_legacy_status_never_implies_economic_eligibility() -> None:
    """Completion without a classification is unavailable, even with a Done label."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    item = model.ObjectToItem(model.data[0])
    column = model.columns["STANDARD_ONLY_COL"]
    model.set_enrichment_status("R1", "Pending")
    assert model.GetValue(item, column) == "◷"
    for status in ("Done", "Classification missing", ""):
        model.set_enrichment_status("R1", status)
        assert model.GetValue(item, column) == "?"
        assert "Retrieving" not in model.get_assembly_tooltip(item)
