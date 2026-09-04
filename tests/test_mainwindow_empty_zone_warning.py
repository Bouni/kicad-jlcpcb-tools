"""Tests for empty-zone warnings during fabrication-data generation."""

import importlib.util
import logging
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

_ROOT = Path(__file__).parent.parent


class _WxModule(types.ModuleType):
    """Minimal wx module whose unused attributes resolve to harmless constants."""

    def __getattr__(self, name):
        """Return a harmless constant for an unused wx attribute."""
        value = 0
        setattr(self, name, value)
        return value


class _Dialog:
    """Stand-in base class used while importing the main window."""


def _stub_module(monkeypatch, name, **attributes):
    """Install a module stub with the requested attributes."""
    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def _load_mainwindow(monkeypatch):
    """Load mainwindow.py under an isolated package with wx dependencies stubbed."""
    wx = _WxModule("wx")
    wx.__path__ = []
    wx.Dialog = _Dialog
    wx.NewIdRef = MagicMock(side_effect=object)
    wx.ID_YES = 1
    wx.ID_NO = 2
    wx.ID_CANCEL = 3
    wx.CANCEL = wx.ID_CANCEL
    wx.OK = 4
    wx.YES_NO = 8
    wx.NO_DEFAULT = 16
    wx.ICON_WARNING = 32
    wx.ICON_ERROR = 64
    wx.CENTER = 128
    wx.BeginBusyCursor = MagicMock()
    wx.EndBusyCursor = MagicMock()
    wx.IsBusy = MagicMock(return_value=True)
    wx.MessageBox = MagicMock()
    wx.MessageDialog = MagicMock()
    monkeypatch.setitem(sys.modules, "wx", wx)

    dataview = _WxModule("wx.dataview")
    adv = _WxModule("wx.adv")
    wx.dataview = dataview
    wx.adv = adv
    monkeypatch.setitem(sys.modules, "wx.dataview", dataview)
    monkeypatch.setitem(sys.modules, "wx.adv", adv)
    monkeypatch.setitem(sys.modules, "pcbnew", MagicMock())

    package_name = f"mainwindow_test_{uuid4().hex}"
    package = types.ModuleType(package_name)
    package.__path__ = [str(_ROOT)]
    monkeypatch.setitem(sys.modules, package_name, package)

    bom_estimation = _stub_module(monkeypatch, f"{package_name}.bom_estimation")
    bom_estimation.__path__ = []
    _stub_module(
        monkeypatch,
        f"{package_name}.bom_estimation.help_text",
        show_bom_estimator_help=MagicMock(),
    )
    enrichment = _stub_module(monkeypatch, f"{package_name}.enrichment")
    enrichment.__path__ = []
    _stub_module(
        monkeypatch,
        f"{package_name}.enrichment.providers",
        LCSCAssemblyMetadataProvider=type("LCSCAssemblyMetadataProvider", (), {}),
    )

    _stub_module(
        monkeypatch,
        f"{package_name}.bom_widget",
        BomEstimatorController=type("BomEstimatorController", (), {}),
        BomEstimatorWidget=type("BomEstimatorWidget", (), {}),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.corrections",
        CorrectionManagerDialog=type("CorrectionManagerDialog", (), {}),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.datamodel",
        PartListDataModel=type("PartListDataModel", (), {"columns": {}}),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.dataview_highlight",
        HighlightedTextRenderer=type("HighlightedTextRenderer", (), {}),
        decode_highlighted_value=MagicMock(),
        simplify_footprint_name=MagicMock(),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.derive_params",
        params_for_part=MagicMock(),
    )

    events = {
        name: object()
        for name in (
            "EVT_ASSEMBLY_ENRICHMENT_COMPLETED_EVENT",
            "EVT_ASSEMBLY_ENRICHMENT_PROGRESS_EVENT",
            "EVT_ASSIGN_PARTS_EVENT",
            "EVT_BOM_DATA_CHANGED_EVENT",
            "EVT_DOWNLOAD_COMPLETED_EVENT",
            "EVT_DOWNLOAD_PROGRESS_EVENT",
            "EVT_DOWNLOAD_STARTED_EVENT",
            "EVT_LOGBOX_APPEND_EVENT",
            "EVT_MESSAGE_EVENT",
            "EVT_POPULATE_FOOTPRINT_LIST_EVENT",
            "EVT_UNZIP_COMBINING_PROGRESS_EVENT",
            "EVT_UNZIP_COMBINING_STARTED_EVENT",
            "EVT_UNZIP_EXTRACTING_COMPLETED_EVENT",
            "EVT_UNZIP_EXTRACTING_PROGRESS_EVENT",
            "EVT_UNZIP_EXTRACTING_STARTED_EVENT",
            "EVT_UPDATE_SETTING",
            "AssemblyEnrichmentCompletedEvent",
            "AssemblyEnrichmentProgressEvent",
            "BomDataChangedEvent",
            "LogboxAppendEvent",
        )
    }
    _stub_module(monkeypatch, f"{package_name}.events", **events)
    _stub_module(
        monkeypatch,
        f"{package_name}.fabrication",
        Fabrication=type("Fabrication", (), {}),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.footprint_helpers",
        get_is_dnp=MagicMock(),
        set_lcsc_value=MagicMock(),
        toggle_exclude_from_bom=MagicMock(),
        toggle_exclude_from_pos=MagicMock(),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.generate_hooks",
        format_hook_error=MagicMock(),
        run_configured_hook=MagicMock(),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.helpers",
        PLUGIN_PATH=str(_ROOT),
        GetScaleFactor=MagicMock(),
        HighResWxSize=MagicMock(),
        getVersion=MagicMock(return_value="test"),
        loadBitmapScaled=MagicMock(),
    )
    _stub_module(
        monkeypatch,
        f"{package_name}.kicad_drc",
        DRCViolationCounter=type("DRCViolationCounter", (), {}),
    )

    class _LibraryState:
        INITIALIZED = object()

    _stub_module(
        monkeypatch,
        f"{package_name}.library",
        Library=type("Library", (), {}),
        LibraryState=_LibraryState,
    )
    for module_name, class_name in (
        ("partdetails", "PartDetailsDialog"),
        ("partmapper", "PartMapperManagerDialog"),
        ("partselector", "PartSelectorDialog"),
        ("schematicexport", "SchematicExport"),
        ("settings", "SettingsDialog"),
        ("store", "Store"),
    ):
        _stub_module(
            monkeypatch,
            f"{package_name}.{module_name}",
            **{class_name: type(class_name, (), {})},
        )

    module_name = f"{package_name}.mainwindow"
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / "mainwindow.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, wx


@pytest.fixture
def mainwindow_module(monkeypatch):
    """Provide an isolated mainwindow module and its wx stub."""
    requests_logger = logging.getLogger("requests")
    urllib3_logger = logging.getLogger("urllib3")
    previous_levels = (requests_logger.level, urllib3_logger.level)
    try:
        yield _load_mainwindow(monkeypatch)
    finally:
        requests_logger.setLevel(previous_levels[0])
        urllib3_logger.setLevel(previous_levels[1])


def _make_window(empty_pours, fill_zones=None):
    """Build the smallest object needed by generate_fabrication_data()."""
    fabrication = SimpleNamespace(
        get_part_consistency_warnings=MagicMock(return_value=""),
        fill_zones=MagicMock(return_value=empty_pours),
        generate_geber=MagicMock(),
        generate_excellon=MagicMock(),
        zip_gerber_excellon=MagicMock(),
        generate_cpl=MagicMock(),
        generate_bom=MagicMock(),
    )
    settings = {"general": {}, "gerber": {}}
    if fill_zones is not None:
        settings["gerber"]["fill_zones"] = fill_zones

    window = SimpleNamespace(
        generate_button=MagicMock(),
        reset_gauge=MagicMock(),
        settings=settings,
        fabrication=fabrication,
        logger=MagicMock(),
        run_drc_before_gerber_export=MagicMock(return_value=True),
        layer_selection=MagicMock(),
        count_order_number_placeholders=MagicMock(return_value=0),
        store=MagicMock(),
        build_generate_hook_env=MagicMock(return_value={}),
        run_generate_hook=MagicMock(return_value=True),
        report_generation_step=MagicMock(),
    )
    window.layer_selection.GetSelection.return_value = 0
    window.layer_selection.GetString.return_value = "Auto"
    window.store.get_generation_count.return_value = 0
    window.store.increment_generation_count.return_value = 1

    generation_steps = []

    def run_generation_step(description, function, *args):
        window._current_generation_step = description
        generation_steps.append(description)
        return function(*args)

    window.run_generation_step = run_generation_step
    return window, generation_steps


def _set_dialog_result(wx, result):
    """Configure and return the fake empty-zone dialog."""
    dialog = MagicMock()
    dialog.ShowModal.return_value = result
    wx.MessageDialog.return_value = dialog
    return dialog


def _warning_text(logger):
    """Render lazy logger arguments into searchable warning text."""
    messages = []
    for logged in logger.warning.call_args_list:
        message, *args = logged.args
        messages.append(message % tuple(args) if args else message)
    return "\n".join(messages)


def test_no_empty_zones_skips_warning_and_completes_generation(mainwindow_module):
    """A board without empty fills proceeds without creating a dialog."""
    mainwindow, wx = mainwindow_module
    window, steps = _make_window([])

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert "Filling copper zones" in steps
    wx.MessageDialog.assert_not_called()
    window.logger.warning.assert_not_called()
    window.run_drc_before_gerber_export.assert_called_once_with()
    window.fabrication.generate_geber.assert_called_once_with(None)
    window.generate_button.Enable.assert_any_call(False)
    window.generate_button.Enable.assert_any_call(True)


@pytest.mark.parametrize(
    ("fill_zones", "expected_step"),
    [
        (None, "Filling copper zones"),
        (False, "Checking copper zone fills"),
    ],
)
def test_continue_logs_all_zones_and_uses_refill_aware_wording(
    mainwindow_module,
    fill_zones,
    expected_step,
):
    """The warning is neutral, complete, and records the continue decision."""
    mainwindow, wx = mainwindow_module
    empty_pours = ["GND on F.Cu", "VCC on In1.Cu"]
    window, steps = _make_window(empty_pours, fill_zones=fill_zones)
    dialog = _set_dialog_result(wx, wx.ID_YES)

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    assert expected_step in steps
    message = wx.MessageDialog.call_args.args[1]
    assert "contain no filled copper" in message
    assert "poured" not in message.lower()
    assert all(pour in message for pour in empty_pours)
    style = wx.MessageDialog.call_args.args[3]
    assert style & wx.NO_DEFAULT
    assert style & wx.ICON_WARNING
    warnings = _warning_text(window.logger)
    assert all(pour in warnings for pour in empty_pours)
    assert "chose to continue export" in warnings
    dialog.SetYesNoLabels.assert_called_once_with("Continue Anyway", "Cancel Export")
    dialog.Destroy.assert_called_once_with()
    window.run_drc_before_gerber_export.assert_called_once_with()
    window.fabrication.generate_geber.assert_called_once_with(None)


@pytest.mark.parametrize("result_name", ["ID_NO", "ID_CANCEL", "unexpected"])
def test_non_affirmative_dialog_results_stop_export(
    mainwindow_module,
    result_name,
):
    """Cancel, close, and unexpected modal results all fail closed."""
    mainwindow, wx = mainwindow_module
    result = 999 if result_name == "unexpected" else getattr(wx, result_name)
    window, _ = _make_window(["GND on F.Cu"])
    dialog = _set_dialog_result(wx, result)

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    window.run_drc_before_gerber_export.assert_not_called()
    window.fabrication.generate_geber.assert_not_called()
    warnings = _warning_text(window.logger)
    assert "GND on F.Cu" in warnings
    assert "chose to stop export" in warnings
    window.report_generation_step.assert_any_call(
        "Export stopped by empty copper zones"
    )
    dialog.Destroy.assert_called_once_with()
    window.generate_button.Enable.assert_any_call(True)


@pytest.mark.parametrize("failing_method", ["SetYesNoLabels", "ShowModal"])
def test_dialog_is_destroyed_when_setup_or_display_raises(
    mainwindow_module,
    failing_method,
):
    """A dialog exception still releases it and restores UI state."""
    mainwindow, wx = mainwindow_module
    window, _ = _make_window(["GND on F.Cu"])
    dialog = MagicMock()
    getattr(dialog, failing_method).side_effect = RuntimeError("dialog failed")
    wx.MessageDialog.return_value = dialog

    mainwindow.JLCPCBTools.generate_fabrication_data(window)

    dialog.Destroy.assert_called_once_with()
    window.run_drc_before_gerber_export.assert_not_called()
    window.logger.exception.assert_called_once()
    wx.MessageBox.assert_called_once()
    assert "dialog failed" in wx.MessageBox.call_args.args[0]
    wx.EndBusyCursor.assert_called_once_with()
    window.generate_button.Enable.assert_any_call(True)
