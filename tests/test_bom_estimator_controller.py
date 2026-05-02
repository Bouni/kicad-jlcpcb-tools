"""Integration tests for BomEstimatorController.recompute.

The controller lives in `bom_widget.py` which imports wx/pcbnew at module
load. We follow the same mock-then-import bootstrap as
`test_bom_designator_split.py` so the controller can be exercised in
non-wx environments.

These tests inject fakes for every callback the controller depends on,
so each scenario is observable without touching real UI or board state.
"""

from pathlib import Path
import sys
import types
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Bootstrap: stub KiCad / wx modules and load bom_widget under a fake package.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).parent.parent

_pcbnew_stub = sys.modules.setdefault("pcbnew", MagicMock())
# Force-set F_Cu even if pcbnew was already stubbed by an earlier test —
# bom_widget._is_on_bottom_side compares against this sentinel.
_pcbnew_stub.F_Cu = 0
for _mod in ["wx", "wx.dataview"]:
    sys.modules.setdefault(_mod, MagicMock())

_pkg_name = "kicadplugin"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [str(_ROOT)]
    sys.modules[_pkg_name] = _pkg

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Load bom_widget under the kicadplugin namespace so its relative imports work.
import importlib  # noqa: E402

bom_widget = importlib.import_module("kicadplugin.bom_widget")
BomEstimatorController = bom_widget.BomEstimatorController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeFootprint:
    def __init__(self, layer=0, flipped=False):
        self._layer = layer
        self._flipped = flipped

    def IsFlipped(self):
        return self._flipped

    def GetLayer(self):
        return self._layer


class _FakeBoard:
    def __init__(self, footprints):
        self._footprints = footprints

    def FindFootprintByReference(self, ref):
        return self._footprints.get(ref)


def _make_controller(
    *,
    parts,
    board=None,
    details_map=None,
    force_standard=False,
):
    """Build a controller with capturing fakes for every callback."""
    captured = {
        "summary_text": [],
        "price_labels": [],
        "trigger_refs": [],
        "refresh_rows_count": 0,
    }

    def refresh_rows():
        captured["refresh_rows_count"] += 1

    def set_summary_text(text):
        captured["summary_text"].append(text)

    def set_price_label(ref, label):
        captured["price_labels"].append((ref, label))

    def set_trigger_refs(refs):
        captured["trigger_refs"].append(set(refs))

    controller = BomEstimatorController(
        read_parts=lambda: parts,
        get_part_details=lambda lcsc: (details_map or {}).get(lcsc, {}),
        get_board=lambda: board if board is not None else _FakeBoard({}),
        is_force_standard_enabled=lambda: force_standard,
        set_price_label=set_price_label,
        set_trigger_refs=set_trigger_refs,
        refresh_rows=refresh_rows,
        set_summary_text=set_summary_text,
    )
    return controller, captured


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_recompute_empty_parts_short_circuits_with_no_parts_summary():
    controller, captured = _make_controller(parts=[])
    controller.recompute(board_count=10)

    assert captured["summary_text"] == ["BOM Estimate (10 boards): no parts"]
    assert captured["trigger_refs"] == [set()]
    assert captured["refresh_rows_count"] == 1
    assert captured["price_labels"] == []


def test_recompute_all_excluded_from_bom_short_circuits_with_no_assigned_summary():
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 1,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
        {
            "reference": "R2",
            "lcsc": "C2",
            "exclude_from_bom": 1,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
    ]
    controller, captured = _make_controller(parts=parts)
    controller.recompute(board_count=5)

    assert captured["summary_text"] == [
        "BOM Estimate (5 boards): no assigned BOM parts"
    ]
    assert captured["trigger_refs"] == [set()]
    assert captured["price_labels"] == []


def test_recompute_all_dnp_short_circuits_when_all_rows_filtered():
    # Parts that survive the bom_parts filter (have lcsc, not excluded) but are
    # all DNP — billable filter inside calculate_bom_estimate drops them but
    # the controller still runs through to the summary path.
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": '{"is_dnp": true}',
        },
    ]
    controller, captured = _make_controller(
        parts=parts,
        board=_FakeBoard({"R1": _FakeFootprint(layer=0)}),
        details_map={"C1": {"price": "1-:0.10", "type": "Basic"}},
    )
    controller.recompute(board_count=5)

    # Summary line is rendered (no short-circuit) but assembly cost should be 0
    # because the only row is DNP.
    assert len(captured["summary_text"]) == 1
    assert "BOM Estimate" in captured["summary_text"][0]
    # Each part still gets a price label set.
    refs_with_labels = {ref for ref, _ in captured["price_labels"]}
    assert refs_with_labels == {"R1"}


def test_recompute_normal_mixed_emits_summary_and_per_part_price_labels():
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
        {
            "reference": "R2",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
        {
            "reference": "U1",
            "lcsc": "C2",
            "exclude_from_bom": 0,
            "pad_count": 8,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
    ]
    board = _FakeBoard(
        {
            "R1": _FakeFootprint(layer=0),
            "R2": _FakeFootprint(layer=0),
            "U1": _FakeFootprint(layer=0),
        }
    )
    details_map = {
        "C1": {"price": "1-:0.05", "type": "Basic"},
        "C2": {"price": "1-:0.40", "type": "Basic"},
    }
    controller, captured = _make_controller(
        parts=parts, board=board, details_map=details_map
    )
    controller.recompute(board_count=10)

    # Summary text gets the multi-line overview/details format.
    assert len(captured["summary_text"]) == 1
    assert "BOM Estimate" in captured["summary_text"][0]
    # Price labels emitted for every distinct reference.
    refs_labelled = {ref for ref, _ in captured["price_labels"]}
    assert refs_labelled == {"R1", "R2", "U1"}
    assert captured["refresh_rows_count"] == 1


def test_get_board_standard_context_multi_side_triggers_when_split_across_sides():
    """multi_side_populated is True when footprints are on both F_Cu and B_Cu."""
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
        {
            "reference": "R2",
            "lcsc": "C2",
            "exclude_from_bom": 0,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
    ]
    # R1 on top (F_Cu == 0), R2 on bottom (any non-zero layer)
    board = _FakeBoard(
        {
            "R1": _FakeFootprint(layer=0),
            "R2": _FakeFootprint(layer=31),
        }
    )
    controller, _ = _make_controller(parts=parts, board=board)
    context = controller._get_board_standard_context(parts, board_count=10)

    assert context["signals"]["multi_side_populated"] is True
    assert context["board_standard"] is True


def test_get_board_standard_context_unified_side_does_not_trigger_multi_side():
    """multi_side_populated is False when all footprints share one side."""
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
        {
            "reference": "R2",
            "lcsc": "C2",
            "exclude_from_bom": 0,
            "has_tht": 0,
            "assembly_flags": "{}",
        },
    ]
    board = _FakeBoard(
        {
            "R1": _FakeFootprint(layer=0),
            "R2": _FakeFootprint(layer=0),
        }
    )
    controller, _ = _make_controller(parts=parts, board=board)
    context = controller._get_board_standard_context(parts, board_count=10)

    assert context["signals"]["multi_side_populated"] is False


def test_recompute_enrichment_pending_uses_available_metadata_and_no_assembly_data():
    # Simulates the enrichment-pending state: get_part_details returns price
    # info but the part rows themselves have no assembly_process / product_type
    # (those would be filled in by enrichment).
    parts = [
        {
            "reference": "R1",
            "lcsc": "C1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            # No assembly_process / component_product_type at all
            "assembly_flags": "{}",
        },
    ]
    board = _FakeBoard({"R1": _FakeFootprint(layer=0)})
    details_map = {"C1": {"price": "1-:0.20", "type": "Basic"}}
    controller, captured = _make_controller(
        parts=parts, board=board, details_map=details_map
    )
    controller.recompute(board_count=8)

    # Controller still produces a summary and per-part labels even with
    # missing enrichment metadata.
    assert len(captured["summary_text"]) == 1
    refs_labelled = {ref for ref, _ in captured["price_labels"]}
    assert refs_labelled == {"R1"}
    assert captured["refresh_rows_count"] == 1
