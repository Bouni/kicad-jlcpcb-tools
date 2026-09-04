"""Integration tests for BomEstimatorController without wx/KiCad."""

import importlib
from pathlib import Path
import sys
import types
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).parent.parent
_pcbnew = sys.modules.setdefault("pcbnew", MagicMock())
_pcbnew.F_Cu = 0
for _module in ("wx", "wx.dataview"):
    sys.modules.setdefault(_module, MagicMock())

_PACKAGE = "kicadplugin"
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_ROOT)]
    sys.modules[_PACKAGE] = package
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

BomEstimatorController = importlib.import_module(
    f"{_PACKAGE}.bom_widget"
).BomEstimatorController


class _Footprint:
    def __init__(self, layer=0):
        self.layer = layer

    def IsFlipped(self):
        return False

    def GetLayer(self):
        return self.layer


class _Board:
    def __init__(self, footprints=None):
        self.footprints = footprints or {}

    def FindFootprintByReference(self, reference):
        return self.footprints.get(reference)


_MISSING = object()


def _part(reference="R1", *, component_product_type=0, **values):
    """Return a placed, billable part with concise per-test overrides."""
    part = {
        "reference": reference,
        "lcsc": "C1",
        "exclude_from_bom": 0,
        "exclude_from_pos": 0,
        "pad_count": 2,
        "has_tht": 0,
        "component_product_type": component_product_type,
        "assembly_flags": "{}",
    }
    part.update(values)
    if component_product_type is _MISSING:
        part.pop("component_product_type")
    return part


def _board(**layers):
    """Build a board from reference=layer entries; None means absent."""
    return _Board(
        {
            reference: _Footprint(layer)
            for reference, layer in layers.items()
            if layer is not None
        }
    )


def _details(**prices):
    return {
        lcsc: {"price": f"1-:{price}", "type": "Basic"}
        for lcsc, price in prices.items()
    }


def _make_controller(*, parts, board=None, details=None, force_standard=False):
    """Build a controller and capture each observable callback."""
    captured = {
        "summary": [],
        "prices": [],
        "standard_refs": [],
    }

    controller = BomEstimatorController(
        read_parts=lambda: parts,
        get_part_details=lambda lcsc: (details or {}).get(lcsc, {}),
        get_board=lambda: board or _Board(),
        is_force_standard_enabled=lambda: force_standard,
        set_price_label=lambda *args: captured["prices"].append(args),
        set_standard_only_refs=lambda refs: captured["standard_refs"].append(set(refs)),
        set_summary_text=captured["summary"].append,
    )
    return controller, captured


@pytest.mark.parametrize(
    ("parts", "board_count", "expected"),
    [
        ([], 10, "BOM Estimate (10 boards): no parts"),
        (
            [_part(exclude_from_bom=1)],
            5,
            "BOM Estimate (5 boards): no assigned BOM parts",
        ),
    ],
)
def test_recompute_empty_states_clear_outputs(parts, board_count, expected):
    """Empty and unassigned BOMs clear every dependent output."""
    controller, captured = _make_controller(parts=parts)

    controller.recompute(board_count)

    assert captured == {
        "summary": [expected],
        "prices": [],
        "standard_refs": [set()],
    }


def test_recompute_dnp_part_still_receives_a_price_label():
    """Keep the table price label current even when its part is DNP."""
    controller, captured = _make_controller(
        parts=[_part(assembly_flags='{"is_dnp": true}')],
        board=_board(R1=0),
        details=_details(C1="0.10"),
    )

    controller.recompute(5)

    assert "BOM Estimate" in captured["summary"][0]
    assert {reference for reference, _ in captured["prices"]} == {"R1"}


def test_recompute_prices_each_reference_in_a_mixed_bom():
    """Price every distinct reference in a mixed BOM."""
    parts = [
        _part("R1"),
        _part("R2"),
        _part("U1", lcsc="C2", pad_count=8),
    ]
    controller, captured = _make_controller(
        parts=parts,
        board=_board(R1=0, R2=0, U1=0),
        details=_details(C1="0.05", C2="0.40"),
    )

    decision = controller.recompute(10)

    assert decision.board_standard is False
    assert {reference for reference, _ in captured["prices"]} == {
        "R1",
        "R2",
        "U1",
    }


def test_recompute_checks_only_standard_only_parts_not_side_context():
    """Only direct Standard Only parts are checked on a two-sided board."""
    parts = [
        _part("U1", component_product_type=2),
        _part("R1", lcsc="C2"),
    ]
    controller, captured = _make_controller(
        parts=parts,
        board=_board(U1=0, R1=31),
        details=_details(C1="0.10", C2="0.10"),
    )

    controller.recompute(5)

    assert captured["standard_refs"] == [{"U1"}]


def test_recompute_clears_standard_indicator_on_later_empty_state():
    """Clear previously checked references when a later scan is empty."""
    parts = [_part("U1", component_product_type=2)]
    controller, captured = _make_controller(
        parts=parts,
        board=_board(U1=0),
        details=_details(C1="0.10"),
    )

    controller.recompute(5)
    parts.clear()
    controller.recompute(5)

    assert captured["standard_refs"] == [{"U1"}, set()]


@pytest.mark.parametrize(
    ("board", "expected"),
    [(_board(R1=0, R2=0), False), (_board(R1=0, R2=31), True)],
    ids=("one-side", "two-sides"),
)
def test_board_context_detects_populated_sides(board, expected):
    """Distinguish one-sided from two-sided placement."""
    parts = [_part("R1"), _part("R2", lcsc="C2")]
    controller, _ = _make_controller(parts=parts, board=board)

    decision = controller._get_board_standard_context(parts, board_count=10)

    assert decision.both_sides_populated is expected
    assert decision.board_standard is expected


@pytest.mark.parametrize(
    ("product_type", "expected_mode", "expected_standard", "expected_missing"),
    [
        (0, False, set(), set()),
        (1, False, set(), set()),
        (2, True, {"U1"}, set()),
        (None, None, set(), {"U1"}),
        ("bad", None, set(), {"U1"}),
        (2.5, None, set(), {"U1"}),
    ],
)
def test_board_context_maps_raw_component_product_types_exactly(
    product_type,
    expected_mode,
    expected_standard,
    expected_missing,
):
    """Only exact type 2 checks the part and forces Standard."""
    parts = [_part("U1", component_product_type=product_type)]
    controller, _ = _make_controller(parts=parts, board=_board(U1=0))

    decision = controller._get_board_standard_context(parts, board_count=5)

    assert decision.board_standard is expected_mode
    assert decision.standard_only_refs == frozenset(expected_standard)
    assert decision.classification_missing_refs == frozenset(expected_missing)


def test_board_context_reports_economic_only_conflict_with_known_trigger():
    """An Economic Only selected part conflicts when quantity requires Standard."""
    parts = [_part("D1", component_product_type=1)]
    controller, _ = _make_controller(parts=parts, board=_board(D1=0))

    decision = controller._get_board_standard_context(parts, board_count=51)

    assert decision.board_standard is True
    assert decision.economic_only_conflict_refs == frozenset({"D1"})


def test_board_context_uses_direct_pos_and_filters_dnp_and_absent_footprints():
    """Use direct POS data and ignore DNP or absent footprints."""
    parts = [
        _part("R1"),
        _part(
            "U1",
            lcsc="C2",
            component_product_type=2,
            exclude_from_pos=1,
            assembly_flags='{"exclude_from_pos": false}',
        ),
        _part(
            "U2",
            lcsc="C3",
            component_product_type=2,
            assembly_flags='{"exclude_from_pos": true}',
        ),
        _part(
            "U3",
            lcsc="C4",
            component_product_type=2,
            assembly_flags='{"is_dnp": true}',
        ),
        _part("U4", lcsc="C5", component_product_type=2),
    ]
    controller, _ = _make_controller(
        parts=parts,
        board=_board(R1=0, U1=0, U2=0, U3=0),
    )

    decision = controller._get_board_standard_context(parts, board_count=5)

    assert decision.standard_only_refs == frozenset({"U2"})
    assert decision.top_refs == frozenset({"R1", "U2"})


def test_recompute_enrichment_pending_uses_available_metadata():
    """Recompute still produces output while assembly enrichment is pending."""
    controller, captured = _make_controller(
        parts=[_part(component_product_type=_MISSING)],
        board=_board(R1=0),
        details=_details(C1="0.20"),
    )

    controller.recompute(8)

    assert len(captured["summary"]) == 1
    assert {reference for reference, _ in captured["prices"]} == {"R1"}
