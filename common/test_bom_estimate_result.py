"""Coherent BOM calculations shared by ordinary and variant presentation."""

from typing import Any
from unittest.mock import Mock

import pytest

from bom_estimation.view import evaluate_bom_estimate

EXPECTED_DISPLAYS = {
    "economic": (False, None, "Estimated pricing mode: Economic"),
    "standard": (True, "Why Standard…", "Estimated pricing mode: Standard"),
    "conflict": (
        True,
        "Review conflict…",
        "Mode conflict · 1 selected part is Economic Only",
    ),
    "unknown": (
        None,
        "Review parts…",
        "Assembly mode unknown · 1 part classification missing",
    ),
    "absent": (None, None, "Assembly mode: N/A"),
}


def _part(reference: str, lcsc: str = "C1", **values: Any) -> dict[str, Any]:
    """Represent a BOM assignment with explicit per-test flag overrides."""
    return {"reference": reference, "lcsc": lcsc, "exclude_from_bom": 0, **values}


def test_price_values_preserve_fractional_precision_and_quantity_tiers() -> None:
    """Deduplicated prices retain zero and joint tiers; only display labels round."""
    parts = [_part("R1"), _part("R2"), _part("R3", "C2"), _part("R4", "C3")]
    catalog = {
        "C1": {"price": "1-9:0.012341,10-:0.0012341"},
        "C2": {"price": "1-:0.0012342"},
        "C3": {"price": "1-:0"},
    }
    details = Mock(side_effect=catalog.__getitem__)
    result = evaluate_bom_estimate(parts, 5, details, sides={})
    prices, labels = result.prices, result.price_labels

    assert prices == {
        "R1": 0.0012341 * 5,
        "R2": 0.0012341 * 5,
        "R3": 0.0012342 * 5,
        "R4": 0.0,
    }
    assert prices["R4"] < prices["R1"] < prices["R3"]
    assert labels == {
        "R1": "$0.0062",
        "R2": "$0.0062",
        "R3": "$0.0062",
        "R4": "$0.0000",
    }
    assert [call.args[0] for call in details.call_args_list] == ["C1", "C2", "C3"]


def test_price_values_preserve_dnp_assignment_prices_and_ignore_bom_exclusions() -> (
    None
):
    """DNP retains its existing assigned-part price; BOM exclusions are unknown."""
    parts = [
        _part("R1"),
        _part("R2", assembly_flags='{"is_dnp": true}'),
        _part("R3", exclude_from_bom=1),
        _part("R4", ""),
        {"lcsc": "C1"},
    ]

    def details(_lcsc: str) -> dict[str, str]:
        return {"price": "1-9:1,10-14:0.5,15-:0.1"}

    result = evaluate_bom_estimate(iter(parts), 5, details, sides={})
    prices, labels = result.prices, result.price_labels

    assert prices == {"R1": 2.5, "R2": 2.5, "R3": None, "R4": None}
    assert labels == {"R1": "$2.5000", "R2": "$2.5000", "R3": "", "R4": ""}


@pytest.mark.parametrize(
    "encoded", ["", "invalid", "1-:-1", "1-:nan", "1-:inf", "1-:-inf"]
)
def test_unavailable_or_nonfinite_prices_never_become_comparable_zero(
    encoded: str,
) -> None:
    """Malformed catalog prices cannot acquire a misleading price direction."""
    prices = evaluate_bom_estimate(
        [_part("R1")], 5, lambda _lcsc: {"price": encoded}, sides={}
    ).prices

    assert prices == {"R1": None}


@pytest.mark.parametrize(
    ("classification", "count", "sides", "forced", "expected"),
    [
        (0, 5, {"R1": "top"}, False, "economic"),
        (2, 5, {"R1": "top"}, False, "standard"),
        (0, 5, {"R1": "top"}, True, "standard"),
        (0, 5, {"R1": "top", "R2": "bottom"}, False, "standard"),
        (1, 51, {"R1": "top"}, False, "conflict"),
        (None, 8, {"R1": "top"}, False, "unknown"),
        (0, 5, {}, True, "absent"),
    ],
    ids=(
        "economic",
        "standard-part",
        "forced",
        "two-sides",
        "conflict",
        "unknown",
        "absent",
    ),
)
def test_estimate_result_keeps_decision_prices_and_presentation_consistent(
    classification: Any,
    count: int,
    sides: dict[str, str],
    forced: bool,
    expected: str,
) -> None:
    """Ordinary and variant consumers receive the same policy and display result."""
    rows = [
        _part(reference, component_product_type=classification)
        for reference in sides or ["R1"]
    ]
    result = evaluate_bom_estimate(
        rows,
        count,
        lambda _: {"price": "1-:0.20", "type": "Basic"},
        sides=sides,
        force_standard=forced,
    )

    mode, button, status = EXPECTED_DISPLAYS[expected]
    assert result.decision.board_standard is mode
    assert result.details_button_label == button
    assert status in result.summary_text
    assert result.decision.standard_only_refs == (
        set(sides) if classification == 2 else set()
    )
    assert result.prices == {row["reference"]: 0.2 * count for row in rows}
    assert result.price_labels == {
        ref: f"${price:.4f}" for ref, price in result.prices.items()
    }
    assert result.summary.component_cost == 0.2 * count * len(rows)
    if classification is None or result.decision.economic_only_conflict_refs:
        assert "Mode-dependent assembly estimate unavailable" in result.summary_text
        assert "Total $" not in result.summary_text
        assert "Direct BOM Cost:" in result.summary_text


@pytest.mark.parametrize(
    "rows", [[], [_part("R1", "")], [_part("R1", exclude_from_bom=1)]]
)
def test_empty_estimate_result_has_no_prices_or_assembly_details(
    rows: list[dict],
) -> None:
    """Empty estimates need neither native placement nor supplier information."""

    def unexpected_details(_: str) -> dict:
        raise AssertionError("An empty BOM must not query catalog data")

    result = evaluate_bom_estimate(
        rows, 5, unexpected_details, sides={}, force_standard=True
    )

    assert result.summary is None
    assert result.prices == result.price_labels == {}
    assert result.decision.board_standard is None
    assert result.decision.manual_enabled
    assert result.details_button_label is None
    assert result.summary_text == "BOM Estimate (5 boards): " + (
        "no assigned BOM parts" if rows else "no parts"
    )
