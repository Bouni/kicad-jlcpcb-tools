"""Tests for BOM estimator presentation/view helpers."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from bom_estimation.pricing import (  # pylint: disable=import-error
    BomEstimateSummary,
    calculate_part_bom_cost,
)
from bom_estimation.view import (  # pylint: disable=import-error
    build_bom_estimate_view_model,
    build_standard_mode_context,
    format_bom_estimate_summary,
    format_part_bom_price_label,
    prepare_bom_price_labels,
    standard_signal_reasons,
)


def test_format_bom_estimate_summary_basic():
    """Economic summary line shows expected fixed/assembly buckets."""
    summary = BomEstimateSummary(
        total_cost=25.50,
        cost_per_board=12.75,
        missing_prices=0,
        component_cost=10.00,
        fixed_cost=8.00,
        extended_cost=3.00,
        economic_setup_cost=8.00,
        standard_setup_cost=0.00,
        stencil_cost=1.50,
        tht_setup_cost=0.00,
        variable_assembly_cost=7.50,
        standard_part_surcharge_cost=0.00,
        smt_joint_count=100,
        tht_joint_count=0,
    )

    overview, details = format_bom_estimate_summary(summary, 2, "Economic", "none")
    assert "Mode Economic" in overview
    assert "Fixed $11.00" in details
    assert "extended: $3.00" in details
    assert "Assembly $7.50" in details


def test_format_bom_estimate_summary_with_standard_surcharge():
    """Standard summary line shows std-parts and hides extended label."""
    summary = BomEstimateSummary(
        total_cost=35.00,
        cost_per_board=17.50,
        missing_prices=2,
        component_cost=10.00,
        fixed_cost=8.00,
        extended_cost=3.00,
        economic_setup_cost=0.00,
        standard_setup_cost=25.00,
        stencil_cost=7.80,
        tht_setup_cost=0.00,
        variable_assembly_cost=12.20,
        standard_part_surcharge_cost=1.50,
        smt_joint_count=50,
        tht_joint_count=10,
    )
    overview, details = format_bom_estimate_summary(
        summary, 1, "Standard", "standard parts"
    )

    assert "Mode Standard" in overview
    assert "std-parts: $1.50" in details
    assert "extended:" not in details
    assert "Assembly $10.70" in details


def test_standard_signal_reasons_orders_labels_consistently():
    """Signal reason labels are ordered for stable display."""
    reasons = standard_signal_reasons(
        {
            "qty_50_plus": True,
            "manual_enabled": True,
            "multi_side_populated": True,
            "v_cut_drawings": False,
            "standard_part_present": True,
        }
    )
    assert reasons == ["manual", "qty≥50", "standard part", "both sides populated"]


def test_standard_signal_reasons_ignores_inactive_flags():
    """No active signals yields no labels."""
    assert standard_signal_reasons({"manual_enabled": False, "qty_50_plus": 0}) == []


def test_calculate_part_bom_cost_uses_raw_component_price_only():
    """Per-part cost helper returns only direct component contribution."""
    part = {"lcsc": "C123", "exclude_from_bom": 0}
    details = {"price": "1-9:0.30,10-:0.20", "type": "Extended"}
    assert calculate_part_bom_cost(part, details, board_count=10) == 2.0


def test_format_part_bom_price_label_handles_missing_and_excluded_parts():
    """Excluded rows render empty per-part labels."""
    assert (
        format_part_bom_price_label(
            {"lcsc": "C1", "exclude_from_bom": 1}, {"price": "1-:0.10"}, board_count=5
        )
        == ""
    )


def test_build_bom_estimate_view_model_returns_summary_and_highlights():
    """View-model builder returns summary and trigger highlights for standard mode."""
    parts = [
        {
            "reference": "R1",
            "lcsc": "C100",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    view_model = build_bom_estimate_view_model(
        parts=parts,
        board_count=5,
        get_part_details=lambda _lcsc: {"price": "1-:1.00", "type": "Basic"},
        standard_context={
            "board_standard": True,
            "smt_populated_sides": 1,
            "signals": {"standard_part_present": True},
            "trigger_references": {"R1"},
        },
    )

    assert view_model["summary"] is not None
    assert view_model["mode"] == "Standard"
    assert view_model["reason_text"] == "standard part"
    assert view_model["highlight_refs"] == {"R1"}


def test_build_standard_mode_context_highlights_standard_parts_and_multiside_refs():
    """Policy context includes all populated refs on multi-side boards."""
    context = build_standard_mode_context(
        manual_enabled=False,
        board_count=5,
        has_v_cut_drawings=False,
        populated_refs={"R1", "R2", "R3"},
        populated_sides={"top", "bottom"},
        smt_populated_sides={"top", "bottom"},
        standard_part_refs={"R2"},
    )

    assert context["trigger_references"] == {"R1", "R2", "R3"}


def test_prepare_bom_price_labels_returns_reference_to_label_mapping():
    """Price-label helper returns expected per-reference mapping."""
    parts = [
        {"reference": "R1", "lcsc": "C123", "exclude_from_bom": 0},
        {"reference": "R2", "lcsc": "C456", "exclude_from_bom": 0},
    ]
    details_store = {
        "C123": {"price": "1-:0.10"},
        "C456": {"price": "5-:0.20"},
    }

    labels = prepare_bom_price_labels(
        parts, board_count=10, get_part_details=lambda lcsc: details_store.get(lcsc, {})
    )

    assert labels == {"R1": "$1.0000", "R2": "$2.0000"}
