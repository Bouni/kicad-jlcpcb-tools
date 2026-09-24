"""Requested example targets are independent of unqualified RF geometry."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from scripts.generate_rf_impedance_fixtures import fixture_files
from tests.rf_impedance_combined import COMBINED_BOARDS, CombinedBoard
from tests.rf_impedance_fixtures import RF_CASES, RFCase

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "impedance"


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_rf_case_uses_requested_single_ended_or_usb_pair_target(case: RFCase) -> None:
    """Targets come from the request, not whatever the fixture currently declares."""
    assert case.target_ohms == ("90" if case.paired else "50")


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_every_rf_layer_specification_keeps_the_requested_target(case: RFCase) -> None:
    """Width-changing transitions retain the target on top, inner, and bottom."""
    (specification,) = case.specifications()
    assert specification.target_ohms == ("90" if case.paired else "50")
    assert {settings.layer for settings in specification.layer_settings} == {
        trace.layer for trace in case.traces()
    }


@pytest.mark.parametrize(
    "case", tuple(case for case in RF_CASES if case.paired), ids=lambda case: case.name
)
def test_differential_examples_use_exact_eight_mil_edge_gap(case: RFCase) -> None:
    """The requested eight mil is an edge gap, not rounded 0.2 mm or centre pitch."""
    assert all(leg.spacing_nm == 203_200 for leg in case.legs)
    assert all(
        setting.spacing_nm == 203_200
        for specification in case.specifications()
        for setting in specification.layer_settings
    )


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_target_changes_do_not_retune_the_native_fixture(case: RFCase) -> None:
    """A requested impedance is metadata, not a solver or geometry adjustment."""
    other_target = replace(case, target_ohms="75")
    assert other_target.board_text() == case.board_text()
    assert other_target.traces() == case.traces()
    assert other_target.legs == case.legs


@pytest.mark.parametrize("board", COMBINED_BOARDS, ids=lambda board: board.name)
def test_combined_banks_share_the_requested_target_and_exact_usb_spacing(
    board: CombinedBoard,
) -> None:
    """Each bank has one class intent covering its independent circuit routes."""
    target = "90" if board.paired else "50"
    assert board.target_ohms == target
    specifications = board.specifications()
    assert len(specifications) == 2
    assert {spec.net_class for spec in specifications} == set(board.net_classes)
    for specification in specifications:
        assert specification.target_ohms == target
        assert {setting.layer for setting in specification.layer_settings} == {
            "F.Cu",
            "In2.Cu",
            "B.Cu",
        }
        assert all(
            setting.spacing_nm == (203_200 if board.paired else None)
            for setting in specification.layer_settings
        )
    for circuit in board.circuits:
        assert circuit.target_ohms == target
        if board.paired:
            assert all(
                profile.spacing_nm == 203_200 for profile in circuit.signal_profiles
            )


@pytest.mark.parametrize("persisted", (False, True), ids=("generated", "on-disk"))
def test_example_manifest_and_index_show_the_requested_targets(persisted: bool) -> None:
    """Regenerated and openable examples agree, including every transition row."""
    files = (
        {
            name: (_EXAMPLES / name).read_text(encoding="utf-8")
            for name in ("manifest.json", "README.md")
        }
        if persisted
        else fixture_files()
    )
    records = json.loads(files["manifest.json"])
    assert {record["name"] for record in records} == {
        "single-ended-50-ohm",
        "usb-differential-90-ohm",
    }
    assert len(records) == 2
    assert sum(record["circuit_count"] for record in records) == 28
    # Single-ended: 12 single-layer routes + 2 three-layer transitions.
    # USB: 12 core/launch profile pairs + 2 three-layer transitions.
    assert {record["name"]: record["expected_reviewed_rows"] for record in records} == {
        "single-ended-50-ohm": 18,
        "usb-differential-90-ohm": 30,
    }
    for record in records:
        target = "90" if record["differential"] else "50"
        assert record["target_ohms"] == target, record["name"]
        table_row = next(
            line
            for line in files["README.md"].splitlines()
            if f"]({record['board']})" in line
        )
        assert f"| {target} Ω |" in table_row
        assert len(record["circuits"]) == record["circuit_count"] == 14
        for circuit in record["circuits"]:
            assert circuit["target_ohms"] == target
            if record["differential"]:
                assert all(leg["pair_edge_gap_mm"] == 0.2032 for leg in circuit["legs"])
    assert "not solver-qualified impedance" in files["README.md"]
    assert "or USB-certified designs" in files["README.md"]
