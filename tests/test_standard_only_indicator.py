"""Tests for the computed Standard-only indicator column."""

import importlib.util
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock, call, patch

_ROOT = Path(__file__).parent.parent
_PACKAGE = "standard_indicator_plugin"


class _DataViewModel:
    ObjectToItem = ItemToObject = staticmethod(lambda value: value)
    HasValue = lambda self, _item, _column: True
    ItemAdded = ItemChanged = ValueChanged = Cleared = lambda self, *_args: None


def _module(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def _load_datamodel():
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


def _row(reference):
    return [reference, "10k", "R_0603", "C1", "Basic", "100"] + ["0"] * 8


def test_indicator_is_read_only_unstyled_and_other_rows_stay_blank():
    """Expose checked read-only cells without changing row styling."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.AddEntry(_row("R2"))
    standard, other = map(model.ObjectToItem, model.data)
    column = model.columns["STANDARD_ONLY_COL"]

    model.set_standard_only_refs({"R1"})

    assert (model.GetColumnCount(), model.GetColumnType(column)) == (16, "bool")
    assert all(len(row) == 15 for row in model.data)
    assert (model.HasValue(standard, column), model.GetValue(standard, column)) == (
        True,
        True,
    )
    assert (model.HasValue(other, column), model.GetValue(other, column)) == (
        False,
        False,
    )
    assert model.SetValue(False, standard, column) is False

    attr = MagicMock()
    assert model.GetAttr(standard, model.columns["REF_COL"], attr) is False
    attr.SetColour.assert_not_called()
    attr.SetBold.assert_not_called()


def test_indicator_state_clears_on_lcsc_reassignment_and_row_reset():
    """Discard stale classification state when rows or assignments change."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.set_standard_only_refs({"R1"})

    model.set_lcsc("R1", "C2", "Basic", "50", "new params")
    assert model.standard_only_refs == set()

    model.set_standard_only_refs({"R1"})
    model.RemoveAll()
    assert model.standard_only_refs == set()


def test_indicator_notifies_only_rows_whose_state_changed():
    """Notify native views precisely, and do nothing for an unchanged set."""
    model = PartListDataModel(scale_factor=1.0)
    model.AddEntry(_row("R1"))
    model.AddEntry(_row("R2"))
    model.ValueChanged = MagicMock()
    column = model.columns["STANDARD_ONLY_COL"]

    model.set_standard_only_refs({"R1"})
    assert model.ValueChanged.call_args_list == [call(model.data[0], column)]

    model.ValueChanged.reset_mock()
    model.set_standard_only_refs({"R1"})
    model.ValueChanged.assert_not_called()

    model.set_standard_only_refs({"R2"})
    assert model.ValueChanged.call_args_list == [
        call(model.data[0], column),
        call(model.data[1], column),
    ]


def test_indicator_tooltip_copy_is_exact():
    """Keep hover guidance terse and stable."""
    assert datamodel.STANDARD_ONLY_TOOLTIP == (
        "Part cannot be assembled in economy mode, standard must be used"
    )
