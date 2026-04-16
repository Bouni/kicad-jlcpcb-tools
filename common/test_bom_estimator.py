"""Tests for BOM estimator business logic."""

from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from bom_estimator import (
    calculate_bom_estimate,
    fetch_assembly_processes,
    get_assembly_flags,
    get_unit_price,
    is_tht_part,
)


def test_get_unit_price_quantity_tiers():
    """Quantity tier parser picks expected unit prices."""
    assert get_unit_price(1, "1-9:0.12,10-99:0.08,100-:0.05") == 0.12
    assert get_unit_price(10, "1-9:0.12,10-99:0.08,100-:0.05") == 0.08
    assert get_unit_price(250, "1-9:0.12,10-99:0.08,100-:0.05") == 0.05


def test_calculate_bom_estimate_smt_and_extended_once_per_lcsc():
    """SMD extended surcharge is charged once per unique extended LCSC."""
    parts = [
        {
            "lcsc": "C123",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "C123",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(_):
        return {"price": "1-9:0.30,10-:0.20", "type": "Extended"}

    summary = calculate_bom_estimate(parts, board_count=5, get_part_details=get_details)

    assert round(summary["component_cost"], 3) == 2.000  # qty=10 at $0.20
    assert round(summary["assembly_cost"], 3) == 44.517  # (5 * $8) + $1.5 stencil + $3 extended + 10 smt joints
    assert round(summary["total_cost"], 3) == 46.517
    assert summary["missing_prices"] == 0
    assert summary["standard_part_count"] == 0


def test_calculate_bom_estimate_tht_setup_and_no_extended_surcharge_for_tht():
    """THT applies setup/joint fees and skips extended surcharge."""
    parts = [
        {
            "lcsc": "C777",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 1,
            "assembly_process": "Wave soldering",
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:0.50", "type": "Extended"}

    summary = calculate_bom_estimate(parts, board_count=5, get_part_details=get_details)

    assert round(summary["component_cost"], 3) == 2.500
    assert round(summary["assembly_cost"], 3) == 43.673  # (5 * 8.00) + 3.50 + (10 * 0.0173)
    assert round(summary["total_cost"], 3) == 46.173


def test_calculate_bom_estimate_missing_price_counts_unknown_lcsc_price():
    """Missing price bands increment diagnostics and keep component cost zero."""
    parts = [
        {
            "lcsc": "C404",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "", "type": "Basic"}

    summary = calculate_bom_estimate(parts, board_count=5, get_part_details=get_details)

    assert summary["component_cost"] == 0.0
    assert summary["missing_prices"] == 1


class _FakeApi:
    def get_part_data(self, lcsc):
        if lcsc == "C1":
            return {
                "success": True,
                "data": {
                    "data": {
                        "assemblyProcess": "SMT",
                        "componentProductType": 2,
                    }
                },
            }
        return {"success": False}


def test_fetch_assembly_processes_uses_api_contract():
    """Assembly fetch helper maps success/failure API responses correctly."""
    result = fetch_assembly_processes(["C1", "C2"], api=_FakeApi())
    assert result == {
        "C1": {
            "assembly_process": "SMT",
            "component_product_type": 2,
            "is_standard_assembly": True,
        },
        "C2": {
            "assembly_process": "",
            "component_product_type": None,
            "is_standard_assembly": False,
        },
    }


def test_get_assembly_flags_handles_bad_json():
    """Malformed assembly flag JSON falls back to an empty dict."""
    assert get_assembly_flags({"assembly_flags": "not-json"}) == {}


def test_is_tht_part_uses_assembly_process_fallback():
    """THT detection falls back to assembly process text when has_tht is missing."""
    assert is_tht_part({"has_tht": None, "assembly_process": "Wave soldering"})
    assert not is_tht_part({"has_tht": None, "assembly_process": "SMT"})


def test_standard_fees_apply_for_standard_smt_part():
    """Standard fixed fees apply for standard parts even when assembly process is SMT."""
    parts = [
        {
            "lcsc": "CSTD1",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=2,
        get_part_details=get_details,
        standard_setup_fee=4.0,
        standard_stencil_fee=1.2,
        standard_part_fee=0.5,
        board_standard=True,
        smt_populated_sides=1,
    )

    assert round(summary["component_cost"], 3) == 2.000
    assert round(summary["assembly_cost"], 3) == 9.707  # (2 * 4.0) + 1.2 + 0.5 + 4*0.0017
    assert summary["standard_part_count"] == 1


def test_standard_fees_are_orthogonal_to_tht_fees():
    """Standard and THT fixed-fee policies can apply together when both conditions are true."""
    parts = [
        {
            "lcsc": "CSTD2",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 1,
            "assembly_process": "THT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "CSMT2",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_lcsc):
        return {"price": "1-:0.80", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=3,
        get_part_details=get_details,
        standard_setup_fee=2.0,
        standard_stencil_fee=0.5,
        standard_part_fee=1.0,
        board_standard=True,
        smt_populated_sides=1,
    )

    assert round(summary["component_cost"], 3) == 4.800
    assert round(summary["assembly_cost"], 3) == 11.109  # 3.5 + (3 * 2.0) + 0.5 + 1.0 + 6*0.0173 + 3*0.0017
    assert summary["standard_part_count"] == 1


def test_standard_fees_do_not_apply_for_non_standard_parts():
    """Non-standard parts do not incur standard fixed fees."""
    parts = [
        {
            "lcsc": "CNONSTD",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_):
        return {"price": "1-:0.50", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=4,
        get_part_details=get_details,
        standard_setup_fee=99.0,
        standard_part_fee=99.0,
        board_standard=False,
    )

    assert round(summary["component_cost"], 3) == 2.000
    assert round(summary["assembly_cost"], 3) == 33.507  # (4 * 8.0) + 1.5 + 4*0.0017
    assert summary["standard_part_count"] == 0


def test_standard_per_side_base_fees_and_all_smt_surcharge_apply():
    """Standard base fees apply per populated SMT side and surcharge all SMT LCSC values."""
    parts = [
        {
            "lcsc": "CSTD3",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 2,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
        {
            "lcsc": "CBASIC3",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        },
    ]

    def get_details(_lcsc):
        return {"price": "1-:1.00", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=1,
        get_part_details=get_details,
        board_standard=True,
        smt_populated_sides=2,
    )

    assert round(summary["component_cost"], 3) == 2.000
    assert round(summary["assembly_cost"], 3) == 68.605  # (25+7.8)*2 + 2*1.5 + 3*0.0017


def test_cost_breakdown_fields_sum_to_total_and_per_board():
    """Breakdown buckets should reconcile with assembly and total costs."""
    parts = [
        {
            "lcsc": "C101",
            "exclude_from_bom": 0,
            "pad_count": 2,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_lcsc):
        return {"price": "1-:1.00", "type": "Extended"}

    summary = calculate_bom_estimate(
        parts,
        board_count=5,
        get_part_details=get_details,
        board_standard=False,
    )

    assert round(summary["component_cost"], 3) == 5.000
    assert round(summary["fixed_cost"], 3) == 41.500  # (5 * 8.0) setup + 1.5 stencil
    assert round(summary["economic_setup_cost"], 3) == 40.000
    assert round(summary["stencil_cost"], 3) == 1.500
    assert round(summary["tht_setup_cost"], 3) == 0.000
    assert round(summary["standard_setup_cost"], 3) == 0.000
    assert round(summary["policy_cost"], 3) == 0.000
    assert round(summary["extended_cost"], 3) == 3.000
    assert round(summary["variable_assembly_cost"], 3) == 0.017  # 10 * 0.0017
    assert round(summary["assembly_cost"], 3) == 44.517
    assert round(summary["total_cost"], 3) == 49.517
    assert round(summary["cost_per_board"], 3) == 9.903


def test_optional_policy_fees_apply_when_configured():
    """Optional policy fees contribute to fixed and total costs."""
    parts = [
        {
            "lcsc": "C201",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_lcsc):
        return {"price": "1-:0.50", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=10,
        get_part_details=get_details,
        board_standard=False,
        order_handling_fee=2.0,
        panelization_per_board_fee=0.1,
        panelization_threshold_boards=10,
    )

    assert round(summary["component_cost"], 3) == 5.000
    assert round(summary["policy_cost"], 3) == 3.000  # 2.0 + (10 * 0.1)
    assert round(summary["fixed_cost"], 3) == 84.500  # (10 * 8.0) + 1.5 + 3.0
    assert round(summary["assembly_cost"], 3) == 84.517
    assert round(summary["total_cost"], 3) == 89.517


def test_panelization_fee_does_not_apply_below_threshold():
    """Panelization per-board fee is skipped when board_count is below threshold."""
    parts = [
        {
            "lcsc": "C301",
            "exclude_from_bom": 0,
            "pad_count": 1,
            "has_tht": 0,
            "assembly_process": "SMT",
            "component_product_type": 0,
            "assembly_flags": '{"exclude_from_pos": false, "is_dnp": false}',
        }
    ]

    def get_details(_lcsc):
        return {"price": "1-:0.50", "type": "Basic"}

    summary = calculate_bom_estimate(
        parts,
        board_count=5,
        get_part_details=get_details,
        board_standard=False,
        order_handling_fee=2.0,
        panelization_per_board_fee=0.1,
        panelization_threshold_boards=10,
    )

    assert round(summary["policy_cost"], 3) == 2.000
    assert round(summary["fixed_cost"], 3) == 43.500  # (5 * 8.0) + 1.5 + 2.0
