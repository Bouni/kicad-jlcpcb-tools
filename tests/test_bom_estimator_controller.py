"""Integration tests for ``BomEstimatorController`` without wx/KiCad."""

from typing import Any

import pytest

from .wx_harness import load_siblings, module

with load_siblings(
    "_bom_estimator_controller",
    ["bom_widget"],
    {
        "wx": module("wx"),
        "pcbnew": module("pcbnew", F_Cu=0),
        "_bom_estimator_controller.helpers": module("helpers", HighResWxSize=object),
    },
) as loaded:
    BomEstimatorController = loaded["bom_widget"].BomEstimatorController


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
def test_recompute_empty_states_clear_outputs(
    parts: list[dict[str, Any]], board_count: int, expected: str
) -> None:
    """Initial and later empty BOMs clear every output, including Standard refs."""
    controller, captured = _make_controller(parts=parts, board=_board(U1=0))

    controller.recompute(board_count)
    empty_outputs = {
        "summary": [expected],
        "button": [None],
        "prices": [],
        "standard_refs": [set()],
    }
    assert captured == empty_outputs
    previous_parts = parts[:]
    parts[:] = [_part("U1", component_product_type=2)]
    controller.recompute(board_count)
    assert captured["standard_refs"][-1] == {"U1"}
    parts[:] = previous_parts
    for values in captured.values():
        values.clear()
    controller.recompute(board_count)
    assert captured == empty_outputs


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


def test_recompute_uses_one_catalog_result_for_summary_and_prices() -> None:
    """Every mixed-BOM row and the summary use the same single read of each LCSC."""
    controller, captured = _make_controller(
        parts=[_part("R1"), _part("R2"), _part("U1", lcsc="C2", pad_count=8)],
        board=_board(R1=0, R2=0, U1=0),
    )
    fetched: list[str] = []

    def get_details(lcsc: str) -> dict[str, str]:
        fetched.append(lcsc)
        return {"price": f"1-:{len(fetched)}", "type": "Basic"}

    controller._get_part_details = get_details
    decision = controller.recompute(5)

    assert decision.board_standard is False
    assert fetched == ["C1", "C2"]
    assert dict(captured["prices"]) == {
        "R1": "$5.0000",
        "R2": "$5.0000",
        "U1": "$10.0000",
    }
    assert "Direct BOM Cost: $20.00" in captured["summary"][0]


@pytest.mark.parametrize(
    ("board", "expected"),
    [(_board(R1=0, R2=0), False), (_board(R1=0, R2=31), True)],
    ids=("one-side", "two-sides"),
)
def test_board_context_detects_populated_sides(board: _Board, expected: bool) -> None:
    """Distinguish one-sided from two-sided placement."""
    parts = [_part("R1"), _part("R2", lcsc="C2")]
    controller, _ = _make_controller(parts=parts, board=board)

    decision = controller.recompute(board_count=10)

    assert decision.both_sides_populated is expected
    assert decision.board_standard is expected


def test_board_context_uses_direct_pos_and_filters_dnp_and_absent_footprints() -> None:
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

    decision = controller.recompute(board_count=5)

    assert decision.standard_only_refs == frozenset({"U2"})
    assert decision.top_refs == frozenset({"R1", "U2"})
