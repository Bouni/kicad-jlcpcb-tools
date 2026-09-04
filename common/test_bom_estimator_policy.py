"""Tests for the modeled JLCPCB assembly-mode decision."""

import pytest

from bom_estimation.assembly_mode import (  # pylint: disable=import-error
    AssemblyModeDecision,
    classify_component_product_type,
)


def _decision(**overrides):
    """Build a decision with one selected dual-mode TOP part by default."""
    inputs = {
        "board_count": 5,
        "top_refs": {"R1"},
        "smt_populated_side_count": 1,
    }
    inputs.update(overrides)
    return AssemblyModeDecision(**inputs)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (0, 0),
        ("1", 1),
        (2, 2),
        (None, None),
        ("bad", None),
        (2.5, None),
        (True, None),
        (3, None),
    ],
)
def test_component_product_type_accepts_only_the_observed_mapping(raw_value, expected):
    """Unknown values never become Standard merely because they are nonzero."""
    assert classify_component_product_type(raw_value) == expected


@pytest.mark.parametrize(
    ("board_count", "standard"), [(49, False), (50, False), (51, True)]
)
def test_quantity_above_50_is_the_standard_boundary(board_count, standard):
    """Economic includes 50 boards; Standard starts at 51."""
    decision = _decision(board_count=board_count)
    assert (decision.quantity_over_50, decision.board_standard) == (
        standard,
        standard,
    )


@pytest.mark.parametrize(
    ("overrides", "mode"),
    [
        ({}, False),
        ({"manual_enabled": True}, True),
        ({"top_refs": set()}, None),
        ({"classification_missing_refs": {"R1"}}, None),
        ({"standard_only_refs": {"R1"}}, True),
        ({"bottom_refs": {"J1"}}, True),
    ],
    ids=("economic", "manual", "no-parts", "missing", "standard-part", "both-sides"),
)
def test_assembly_mode_scenarios(overrides, mode):
    """Independent policy signals select Economic, Standard, or unknown."""
    assert _decision(**overrides).board_standard is mode


def test_decision_retains_refs_and_reports_only_active_conflicts():
    """Normalized facts retain explanation context and active conflicts."""
    decision = _decision(
        top_refs={"U1", "Q2"},
        bottom_refs={"J1"},
        standard_only_refs={"U1"},
        economic_only_refs={"J1"},
        classification_missing_refs={"Q2"},
    )

    assert decision.standard_only_refs == {"U1"}
    assert decision.classification_missing_refs == {"Q2"}
    assert decision.economic_only_conflict_refs == {"J1"}
    assert decision.both_sides_populated
    assert (decision.top_refs, decision.bottom_refs) == ({"U1", "Q2"}, {"J1"})
    assert decision.smt_populated_side_count == 1
    assert not _decision(economic_only_refs={"R1"}).economic_only_conflict_refs
