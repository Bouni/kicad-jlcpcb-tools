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
from unittest.mock import MagicMock

import pytest

from .wx_harness import load_mainwindow, wx_stubs

_ids = count(1)
mainwindow = load_mainwindow(
    "mainwindow_stale_footprint_tests",
    wx=wx_stubs(
        Frame=type("Frame", (), {}),
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
    window._displayed_part_uuids = {ref: ref for ref in footprints}
    window._refresh_footprints_preserving_selection = MagicMock()
    window.test_assignment_batches = []
    window.test_assignments = {}

    def set_lcsc_assignments(
        assignments: Iterable[tuple[str, str, Optional[int]]],  # noqa: UP045
        **_kwargs: Any,
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


def _part(reference):
    """Return the complete store row consumed by populate_footprint_list."""
    return {
        "reference": reference,
        "footprint_uuid": reference,
        "is_dnp": False,
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

    JLCPCBTools.populate_footprint_list(window)

    added_references = [
        invocation.args[0][0]
        for invocation in window.partlist_data_model.AddEntry.call_args_list
    ]
    assert added_references == ["R2"]


def test_assignment_with_deleted_reference_rejects_entire_action() -> None:
    """A stale selector must not partially apply its intended selection."""
    window = _window(footprints={"R2": _LiveFootprint()})
    JLCPCBTools.assign_parts(
        window,
        types.SimpleNamespace(
            references=["R_REMOVED", "R2"], lcsc="C12345", type="Basic", stock=27
        ),
    )
    window.store.set_lcsc_assignments.assert_not_called()
    window.partlist_data_model.set_lcsc.assert_not_called()
    window.start_assembly_enrichment.assert_not_called()
    assert "Selected footprints changed" in str(window.logger.warning.call_args)


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
def test_toggle_handlers_reject_selection_with_deleted_reference(
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
    window.store.read_all.return_value = [_part("R2")]

    getattr(JLCPCBTools, handler_name)(window)

    window.store.update_parts.assert_not_called()
    window._refresh_footprints_preserving_selection.assert_not_called()
    assert "Selected footprints changed" in str(window.logger.warning.call_args)
