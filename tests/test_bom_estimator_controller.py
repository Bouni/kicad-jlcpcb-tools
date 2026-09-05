"""Integration tests for ``BomEstimatorController`` without wx/KiCad."""

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
    """Build a board from ``reference=layer`` entries; ``None`` means absent."""
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
        "button": [],
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
        set_details_button_label=captured["button"].append,
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
        "button": [None],
        "prices": [],
        "standard_refs": [set()],
    }


def test_recompute_dnp_part_still_receives_a_price_label():
    """Keep the table's price label current even when its part is DNP."""
    controller, captured = _make_controller(
        parts=[_part(assembly_flags='{"is_dnp": true}')],
        board=_board(R1=0),
        details=_details(C1="0.10"),
    )

    controller.recompute(5)

    assert "BOM Estimate" in captured["summary"][0]
    assert {reference for reference, _ in captured["prices"]} == {"R1"}
    assert captured["button"] == [None]


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


@pytest.mark.parametrize(
    ("options", "board_count", "expected"),
    [
        pytest.param(
            {},
            5,
            (False, set(), None, "Estimated pricing mode: Economic"),
            id="economic",
        ),
        pytest.param(
            {
                "parts": [
                    _part("U1", component_product_type=2),
                    _part("R1", lcsc="C2"),
                ],
                "board": _board(U1=0, R1=0),
            },
            5,
            (True, {"U1"}, "Why Standard…", "Estimated pricing mode: Standard"),
            id="standard-only-part",
        ),
        pytest.param(
            {"force_standard": True},
            5,
            (True, set(), "Why Standard…", "Estimated pricing mode: Standard"),
            id="manual-standard",
        ),
        pytest.param(
            {
                "parts": [_part("D1", component_product_type=1)],
                "board": _board(D1=0),
            },
            51,
            (
                True,
                set(),
                "Review conflict…",
                "Mode conflict · 1 selected part is Economic Only",
            ),
            id="economic-only-conflict",
        ),
        pytest.param(
            {"parts": [_part(component_product_type=_MISSING)]},
            8,
            (
                None,
                set(),
                "Review parts…",
                "Assembly mode unknown · 1 part classification missing",
            ),
            id="missing-classification",
        ),
    ],
)
def test_recompute_assembly_mode_scenarios(options, board_count, expected):
    """Apply each policy outcome to indicator, button, and summary callbacks."""
    defaults = {"parts": [_part()], "board": _board(R1=0)}
    controller, captured = _make_controller(**(defaults | options))
    expected_mode, expected_refs, expected_button, summary_fragment = expected

    decision = controller.recompute(board_count)

    assert decision.board_standard is expected_mode
    assert captured["standard_refs"] == [expected_refs]
    assert captured["button"] == [expected_button]
    assert summary_fragment in captured["summary"][0]
    assert "Triggers" not in captured["summary"][0]
    assert "Standard because" not in captured["summary"][0]


def test_recompute_clears_standard_indicator_on_later_empty_state():
    """Clear previously checked references when a later scan is empty."""
    parts = [_part("U1", component_product_type=2)]
    controller, captured = _make_controller(parts=parts, board=_board(U1=0))

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


def test_missing_classification_keeps_direct_cost_but_suppresses_mode_totals():
    """Keep known component cost while hiding an unknown assembly total."""
    controller, captured = _make_controller(
        parts=[_part(component_product_type=_MISSING)],
        board=_board(R1=0),
        details=_details(C1="0.20"),
    )

    controller.recompute(8)

    summary = captured["summary"][0]
    assert "Direct BOM Cost: $1.60" in summary
    assert "Mode-dependent assembly estimate unavailable" in summary
    assert "Total $" not in summary
