"""Regression tests for main-window actions targeting deleted footprints.

``mainwindow.py`` normally runs inside KiCad and imports wxPython/pcbnew at
module load time.  These tests load it under a private synthetic package and
provide only the dependency surface needed by the handlers under test.  The
handlers themselves are invoked directly against small capturing fakes, so the
tests exercise production control flow without requiring a GUI event loop.
"""

from contextlib import contextmanager
import importlib.util
from itertools import count
import logging
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock, call

import pytest

_ROOT = Path(__file__).parent.parent
_PACKAGE = "pr782_stale_footprint_tests"
_MISSING = object()


def _module(name, **symbols):
    """Create a module containing the explicitly supplied symbols."""
    module = types.ModuleType(name)
    module.__dict__.update(symbols)
    return module


@contextmanager
def _temporary_modules(replacements):
    """Install import stubs temporarily and restore prior modules afterward."""
    previous = {name: sys.modules.get(name, _MISSING) for name in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _load_mainwindow_module():
    """Load mainwindow with deterministic stubs for its GUI dependencies."""
    package = _module(_PACKAGE)
    package.__path__ = [str(_ROOT)]

    bom_estimation_package = _module(f"{_PACKAGE}.bom_estimation")
    bom_estimation_package.__path__ = []
    enrichment_package = _module(f"{_PACKAGE}.enrichment")
    enrichment_package.__path__ = []

    class _Dialog:
        pass

    ids = count(1)
    wx = _module(
        "wx",
        Dialog=_Dialog,
        NewIdRef=lambda: next(ids),
        PostEvent=lambda *_args, **_kwargs: None,
    )
    wx.__path__ = []
    wx_dataview = _module("wx.dataview")
    wx_adv = _module("wx.adv")
    wx.dataview = wx_dataview
    wx.adv = wx_adv

    def event_factory(**values):
        return types.SimpleNamespace(**values)

    event_names = (
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
    )
    events = {name: object() for name in event_names}
    events.update(
        {
            "AssemblyEnrichmentCompletedEvent": event_factory,
            "AssemblyEnrichmentProgressEvent": event_factory,
            "BomDataChangedEvent": event_factory,
            "LogboxAppendEvent": event_factory,
        }
    )

    library_state = types.SimpleNamespace(
        INITIALIZED=object(),
        UPDATE_NEEDED=object(),
    )
    replacements = {
        "pcbnew": _module("pcbnew"),
        "wx": wx,
        "wx.dataview": wx_dataview,
        "wx.adv": wx_adv,
        _PACKAGE: package,
        f"{_PACKAGE}.bom_estimation": bom_estimation_package,
        f"{_PACKAGE}.bom_estimation.help_text": _module(
            f"{_PACKAGE}.bom_estimation.help_text",
            show_bom_estimator_help=lambda *_args, **_kwargs: None,
        ),
        f"{_PACKAGE}.bom_widget": _module(
            f"{_PACKAGE}.bom_widget",
            BomEstimatorController=object,
            BomEstimatorWidget=object,
        ),
        f"{_PACKAGE}.corrections": _module(
            f"{_PACKAGE}.corrections", CorrectionManagerDialog=object
        ),
        f"{_PACKAGE}.datamodel": _module(
            f"{_PACKAGE}.datamodel", PartListDataModel=object
        ),
        f"{_PACKAGE}.dataview_highlight": _module(
            f"{_PACKAGE}.dataview_highlight",
            HighlightedTextRenderer=object,
            decode_highlighted_value=lambda value: (value, []),
            simplify_footprint_name=lambda value: value,
        ),
        f"{_PACKAGE}.derive_params": _module(
            f"{_PACKAGE}.derive_params",
            params_for_part=lambda _details: "params",
        ),
        f"{_PACKAGE}.enrichment": enrichment_package,
        f"{_PACKAGE}.enrichment.providers": _module(
            f"{_PACKAGE}.enrichment.providers",
            LCSCAssemblyMetadataProvider=object,
        ),
        f"{_PACKAGE}.events": _module(f"{_PACKAGE}.events", **events),
        f"{_PACKAGE}.fabrication": _module(
            f"{_PACKAGE}.fabrication", Fabrication=object
        ),
        f"{_PACKAGE}.footprint_helpers": _module(
            f"{_PACKAGE}.footprint_helpers",
            get_is_dnp=lambda _footprint: False,
            set_lcsc_value=lambda *_args: None,
            toggle_exclude_from_bom=lambda _footprint: None,
            toggle_exclude_from_pos=lambda _footprint: None,
        ),
        f"{_PACKAGE}.generate_hooks": _module(
            f"{_PACKAGE}.generate_hooks",
            format_hook_error=lambda result: str(result),
            run_configured_hook=lambda **_kwargs: None,
        ),
        f"{_PACKAGE}.helpers": _module(
            f"{_PACKAGE}.helpers",
            PLUGIN_PATH=str(_ROOT),
            GetScaleFactor=lambda _window: 1,
            HighResWxSize=lambda _window, size: size,
            getVersion=lambda: "test",
            loadBitmapScaled=lambda *_args: None,
        ),
        f"{_PACKAGE}.kicad_drc": _module(
            f"{_PACKAGE}.kicad_drc", DRCViolationCounter=object
        ),
        f"{_PACKAGE}.library": _module(
            f"{_PACKAGE}.library", Library=object, LibraryState=library_state
        ),
        f"{_PACKAGE}.partdetails": _module(
            f"{_PACKAGE}.partdetails", PartDetailsDialog=object
        ),
        f"{_PACKAGE}.partmapper": _module(
            f"{_PACKAGE}.partmapper", PartMapperManagerDialog=object
        ),
        f"{_PACKAGE}.partselector": _module(
            f"{_PACKAGE}.partselector", PartSelectorDialog=object
        ),
        f"{_PACKAGE}.schematicexport": _module(
            f"{_PACKAGE}.schematicexport", SchematicExport=object
        ),
        f"{_PACKAGE}.settings": _module(
            f"{_PACKAGE}.settings", SettingsDialog=object
        ),
        f"{_PACKAGE}.store": _module(f"{_PACKAGE}.store", Store=object),
    }

    module_name = f"{_PACKAGE}.mainwindow"
    spec = importlib.util.spec_from_file_location(module_name, _ROOT / "mainwindow.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PACKAGE
    replacements[module_name] = module

    logger_levels = {
        name: logging.getLogger(name).level for name in ("requests", "urllib3")
    }
    try:
        with _temporary_modules(replacements):
            spec.loader.exec_module(module)
    finally:
        for name, level in logger_levels.items():
            logging.getLogger(name).setLevel(level)
    return module


mainwindow = _load_mainwindow_module()
JLCPCBTools = mainwindow.JLCPCBTools


class _LiveFootprint:
    """Small live-footprint sentinel used by list population."""

    def __init__(self, layer=0):
        self.layer = layer

    def GetLayer(self):
        return self.layer


class _Board:
    def __init__(self, footprints):
        self.footprints = dict(footprints)

    def FindFootprintByReference(self, reference):
        return self.footprints.get(reference)


class _Pcbnew:
    def __init__(self, board):
        self.board = board

    def GetBoard(self):
        return self.board


def _window(*, footprints, selections=()):
    """Build the shared state surface used by main-window action handlers."""
    window = object.__new__(JLCPCBTools)
    window.pcbnew = _Pcbnew(_Board(footprints))
    window.store = MagicMock()
    window.library = MagicMock()
    window.library.get_part_details.return_value = {}
    window.library.get_all_correction_data.return_value = []
    window.partlist_data_model = MagicMock()
    window.footprint_list = MagicMock()
    window.footprint_list.GetSelections.return_value = list(selections)
    window.start_assembly_enrichment = MagicMock()
    window.logger = MagicMock()
    return window


def _part(reference):
    """Return the complete store row consumed by populate_footprint_list."""
    return {
        "reference": reference,
        "value": "10k",
        "footprint": "R_0603",
        "lcsc": "",
        "stock": None,
        "exclude_from_bom": 0,
        "exclude_from_pos": 0,
        "assembly_process": "",
        "component_product_type": None,
    }


def test_populate_footprint_list_skips_stale_row_and_retains_live_row(monkeypatch):
    """A refresh must omit deleted store rows without losing live rows."""
    live_footprint = _LiveFootprint()
    window = _window(footprints={"R2": live_footprint})
    window.store.read_all.return_value = [_part("R_REMOVED"), _part("R2")]
    window.hide_bom_parts = False
    window.hide_pos_parts = False
    window.get_correction = MagicMock(return_value="0°, 0.0/0.0")
    window._get_enrichment_status_label = MagicMock(return_value="")
    monkeypatch.setattr(mainwindow, "get_is_dnp", lambda _footprint: False)

    JLCPCBTools.populate_footprint_list(window)

    added_references = [
        invocation.args[0][0]
        for invocation in window.partlist_data_model.AddEntry.call_args_list
    ]
    assert added_references == ["R2"]


def test_assign_parts_skips_stale_refs_and_continues_live_refs():
    """Assignment must mutate and enrich only references still on the board."""
    live_footprint = _LiveFootprint()
    window = _window(footprints={"R2": live_footprint})
    event = types.SimpleNamespace(
        lcsc="C12345",
        stock="27",
        type="Basic",
        references=["R_REMOVED", "R2"],
    )

    JLCPCBTools.assign_parts(window, event)

    observed = {
        "store_lcsc": window.store.set_lcsc.call_args_list,
        "store_stock": window.store.set_stock.call_args_list,
        "model_lcsc": window.partlist_data_model.set_lcsc.call_args_list,
        "enrichment": window.start_assembly_enrichment.call_args_list,
    }
    expected = {
        "store_lcsc": [call("R2", "C12345")],
        "store_stock": [call("R2", 27)],
        "model_lcsc": [call("R2", "C12345", "Basic", "27", "params")],
        "enrichment": [call(["R2"])],
    }
    assert observed == expected


def test_assign_parts_with_only_stale_refs_does_not_start_enrichment():
    """An all-stale selector result must be a no-op, including enrichment."""
    window = _window(footprints={})
    event = types.SimpleNamespace(
        lcsc="C12345",
        stock="27",
        type="Basic",
        references=["R_REMOVED"],
    )

    JLCPCBTools.assign_parts(window, event)

    observed = {
        "store_lcsc": window.store.set_lcsc.call_args_list,
        "store_stock": window.store.set_stock.call_args_list,
        "model_lcsc": window.partlist_data_model.set_lcsc.call_args_list,
        "enrichment": window.start_assembly_enrichment.call_args_list,
    }
    assert observed == {
        "store_lcsc": [],
        "store_stock": [],
        "model_lcsc": [],
        "enrichment": [],
    }


@pytest.mark.parametrize("handler_name", ["toggle_bom", "toggle_pos", "toggle_bom_pos"])
def test_toggle_handlers_skip_stale_refs_and_continue_live_refs(
    monkeypatch, handler_name
):
    """BOM/POS actions must not mutate store or model state for deleted rows."""
    stale_item = object()
    live_item = object()
    live_footprint = _LiveFootprint()
    window = _window(
        footprints={"R2": live_footprint},
        selections=[stale_item, live_item],
    )
    references = {stale_item: "R_REMOVED", live_item: "R2"}
    window.partlist_data_model.get_reference.side_effect = references.__getitem__
    monkeypatch.setattr(
        mainwindow,
        "toggle_exclude_from_bom",
        lambda footprint: None if footprint is None else True,
    )
    monkeypatch.setattr(
        mainwindow,
        "toggle_exclude_from_pos",
        lambda footprint: None if footprint is None else True,
    )

    getattr(JLCPCBTools, handler_name)(window)

    expected = {
        "store_bom": [],
        "store_pos": [],
        "model_bom": [],
        "model_pos": [],
        "model_bom_pos": [],
    }
    if handler_name in {"toggle_bom", "toggle_bom_pos"}:
        expected["store_bom"] = [call("R2", 1)]
    if handler_name in {"toggle_pos", "toggle_bom_pos"}:
        expected["store_pos"] = [call("R2", 1)]
    expected[f"model_{handler_name.removeprefix('toggle_')}"] = [call(live_item)]

    observed = {
        "store_bom": window.store.set_bom.call_args_list,
        "store_pos": window.store.set_pos.call_args_list,
        "model_bom": window.partlist_data_model.toggle_bom.call_args_list,
        "model_pos": window.partlist_data_model.toggle_pos.call_args_list,
        "model_bom_pos": window.partlist_data_model.toggle_bom_pos.call_args_list,
    }
    assert observed == expected
