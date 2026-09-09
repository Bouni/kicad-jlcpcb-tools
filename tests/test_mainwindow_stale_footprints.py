"""Regression tests for main-window actions targeting deleted footprints.

``mainwindow.py`` normally runs inside KiCad and imports wxPython/pcbnew at
module load time.  The shared harness loads it under a private synthetic
package instead.  The handlers themselves are invoked directly against small
capturing fakes, so the tests exercise production control flow without
requiring a GUI event loop.
"""

from collections.abc import Iterable
from itertools import count
import types
from typing import Any, Optional
from unittest.mock import MagicMock, call

import pytest

from .wx_harness import load_mainwindow, wx_stubs

_ids = count(1)
mainwindow = load_mainwindow(
    "mainwindow_stale_footprint_tests",
    wx=wx_stubs(
        Dialog=type("Dialog", (), {}),
        NewIdRef=lambda: next(_ids),
        PostEvent=lambda *_args, **_kwargs: None,
    ),
)
JLCPCBTools = mainwindow.JLCPCBTools


class _LiveFootprint:
    """Small live-footprint sentinel used by list population."""

    def __init__(self, layer=0):
        self.layer = layer

    def GetLayer(self):
        return self.layer

    def GetFPID(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(GetLibItemName=lambda: "R_0603")

    def GetValue(self) -> str:
        return "10k"


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


def _window(*, footprints: dict[str, object], selections: tuple[int, ...] = ()) -> Any:
    """Build the shared state surface used by main-window action handlers."""
    window = object.__new__(JLCPCBTools)
    window.pcbnew = _Pcbnew(_Board(footprints))
    window.store = MagicMock()
    window.test_assignment_batches = []
    window.test_assignments = {}

    def set_lcsc_assignments(
        assignments: Iterable[tuple[str, str, Optional[int]]],  # noqa: UP045
    ) -> None:
        batch = list(assignments)
        window.test_assignment_batches.append(batch)
        window.test_assignments.update(
            {reference: (lcsc, stock) for reference, lcsc, stock in batch}
        )

    window.store.set_lcsc_assignments.side_effect = set_lcsc_assignments
    window.settings = {}
    window.library = MagicMock()
    window.library.get_part_details.return_value = {}
    window.library.read_correction_data.return_value = types.SimpleNamespace(
        corrections=(), state=mainwindow.CorrectionState.READY
    )
    window.correction_status = MagicMock()
    window.Layout = MagicMock()
    window.partlist_data_model = MagicMock()
    window.footprint_list = MagicMock()
    window.footprint_list.GetSelections.return_value = list(selections)
    window.start_assembly_enrichment = MagicMock()
    window.logger = MagicMock()
    return window


def _part(reference, lcsc=""):
    """Return the complete store row consumed by populate_footprint_list."""
    return {
        "reference": reference,
        "value": "10k",
        "footprint": "R_0603",
        "lcsc": lcsc,
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


def test_assign_parts_skips_stale_refs_and_continues_live_refs() -> None:
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
        "store_assignments": window.test_assignment_batches,
        "store_lcsc": window.store.set_lcsc.call_args_list,
        "store_stock": window.store.set_stock.call_args_list,
        "model_lcsc": window.partlist_data_model.set_lcsc.call_args_list,
        "enrichment": window.start_assembly_enrichment.call_args_list,
    }
    expected = {
        "store_assignments": [[("R2", "C12345", 27)]],
        "store_lcsc": [],
        "store_stock": [],
        "model_lcsc": [call("R2", "C12345", "Basic", "27", "params")],
        "enrichment": [call(["R2"])],
    }
    assert observed == expected
    window.store.set_lcsc_assignments.assert_called_once()
    assert window.test_assignments == {"R2": ("C12345", 27)}


def test_assign_parts_with_only_stale_refs_does_not_start_enrichment() -> None:
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
        "store_assignments": window.test_assignment_batches,
        "store_lcsc": window.store.set_lcsc.call_args_list,
        "store_stock": window.store.set_stock.call_args_list,
        "model_lcsc": window.partlist_data_model.set_lcsc.call_args_list,
        "enrichment": window.start_assembly_enrichment.call_args_list,
    }
    assert observed == {
        "store_assignments": [],
        "store_lcsc": [],
        "store_stock": [],
        "model_lcsc": [],
        "enrichment": [],
    }
    window.store.set_lcsc_assignments.assert_not_called()
    assert window.test_assignments == {}


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


def test_populate_footprint_list_looks_a_part_up_once_per_part(monkeypatch):
    """Two spellings of one number are one part, so one lookup serves both.

    The details cache is keyed on the canonical number rather than on the raw
    column, so a store holding "C12345" on one row and " c12345 " on another
    -- which it can, since several paths write the column unnormalised -- does
    not fetch the same part twice, and both rows get the details.
    """
    window = _window(footprints={"R1": _LiveFootprint(), "R2": _LiveFootprint()})
    window.hide_bom_parts = False
    window.hide_pos_parts = False
    window.pending_assembly_enrichment = set()
    window.store.read_all.return_value = [
        _part("R1", "C12345"),
        _part("R2", " c12345 "),
    ]
    window.library.get_part_details.return_value = {"type": "Basic", "stock": "27"}

    JLCPCBTools.populate_footprint_list(window)

    lookups = window.library.get_part_details.call_args_list
    assert len(lookups) == 1, f"expected one lookup per part, got {lookups}"
    assert str(lookups[0].args[0]) == "C12345"

    rows = window.partlist_data_model.AddEntry.call_args_list
    assert len(rows) == 2
    assert [row.args[0][4] for row in rows] == ["Basic", "Basic"]
