"""Openable RF projects declare intent independently of physical route width."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from impedance.matching import analyze
from impedance.model import Config
from scripts.generate_rf_impedance_fixtures import fixture_files, project_text
from tests import test_impedance_capture_examples as capture_examples
from tests.rf_impedance_combined import COMBINED_BOARDS, CombinedBoard
from tests.rf_impedance_fixtures import COPPER_LAYERS, RF_CASES, RFCase

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "impedance"
rf_examples = capture_examples.rf_examples


def _expected_class(case: RFCase) -> str:
    """Pin the requested targets independently of generated fixture metadata."""
    return "USB differential 90 ohm" if case.paired else "RF single-ended 50 ohm"


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_native_rf_project_assigns_only_its_signal_nets_to_the_engineering_class(
    case: RFCase,
) -> None:
    """All short, long and transitioning RF cases are selectable by exact class."""
    settings = json.loads(project_text(case))["net_settings"]
    assert settings["meta"]["version"] == 5
    classes = {item["name"]: item for item in settings["classes"]}
    expected_name = _expected_class(case)
    assert set(classes) == {"Default", expected_name}
    assert classes[expected_name]["priority"] < classes["Default"]["priority"]
    assert classes[expected_name]["tuning_profile"] == ""
    assert settings["netclass_patterns"] == [
        {"pattern": net, "netclass": expected_name}
        for net in (("USB_D-", "USB_D+") if case.paired else ("RF_SE",))
    ]
    # These are standalone boards, not cached schematic-label assignments.
    assert settings["netclass_assignments"] == {}
    assert "GND" not in {item["pattern"] for item in settings["netclass_patterns"]}
    assert all(
        not any(token in item["pattern"] for token in "*?[]")
        for item in settings["netclass_patterns"]
    )
    rf_class = classes[expected_name]
    first_profile = case.signal_profiles[0]
    assert rf_class["track_width"] == first_profile.width_nm / 1_000_000
    assert rf_class["clearance"] == 0.2
    assert (rf_class["via_diameter"], rf_class["via_drill"]) == (0.6, 0.3)
    if case.paired:
        assert rf_class["diff_pair_width"] == first_profile.width_nm / 1_000_000
        assert rf_class["diff_pair_gap"] == 0.2032


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_fixture_class_metadata_covers_every_routed_layer_and_pair_member(
    case: RFCase,
) -> None:
    """Class intent follows the electrical nets through changes in width/layer."""
    assert case.net_class == _expected_class(case)
    assert case.net_names == (("USB_D-", "USB_D+") if case.paired else ("RF_SE",))
    assert {trace.net for trace in case.traces()} == set(case.net_names)
    for leg in case.legs:
        assert {
            trace.net for trace in case.traces() if trace.layer == leg.layer
        } == set(case.net_names)
    # One engineering intent follows the class through core and launch layers.
    (specification,) = case.specifications()
    assert specification.net_class == _expected_class(case)
    assert specification.target_ohms == ("90" if case.paired else "50")
    assert {settings.layer for settings in specification.layer_settings} == {
        trace.layer for trace in case.traces()
    }


@pytest.mark.parametrize("persisted", (False, True), ids=("generated", "on-disk"))
def test_inspection_manifest_names_the_engineering_class_and_exact_members(
    persisted: bool,
) -> None:
    """Users can find the class without interpreting source-code selectors."""
    source = (
        (_EXAMPLES / "manifest.json").read_text(encoding="utf-8")
        if persisted
        else fixture_files()["manifest.json"]
    )
    records = {item["name"]: item for item in json.loads(source)}
    assert set(records) == {"single-ended-50-ohm", "usb-differential-90-ohm"}
    for board in COMBINED_BOARDS:
        record = records[board.name]
        circuits = {item["name"]: item for item in record["circuits"]}
        assert len(circuits) == 14
        assert record["net_classes"] == {
            name: list(nets) for name, nets in board.net_classes.items()
        }
        for circuit in board.circuits:
            assert circuits[circuit.name]["net_class"] == circuit.net_class
            assert circuits[circuit.name]["net_names"] == list(circuit.net_names)


@pytest.mark.parametrize("persisted", (False, True), ids=("generated", "on-disk"))
@pytest.mark.parametrize("board", COMBINED_BOARDS, ids=lambda board: board.name)
def test_two_openable_projects_assign_each_circuit_to_its_bank_class(
    board: CombinedBoard, persisted: bool
) -> None:
    """Combining boards must not merge circuit nets or classify ground as RF."""
    name = f"{board.name}.kicad_pro"
    source = (
        (_EXAMPLES / name).read_text(encoding="utf-8")
        if persisted
        else fixture_files()[name]
    )
    settings = json.loads(source)["net_settings"]
    classes = {item["name"]: item for item in settings["classes"]}
    prefix = "USB 90 ohm" if board.paired else "RF 50 ohm"
    assert set(classes) == {"Default", f"{prefix} CPWG", f"{prefix} noncoplanar"}
    # KiCad normalizes an empty assignment map to null when saving a project.
    assert settings["netclass_assignments"] in ({}, None)
    patterns = settings["netclass_patterns"]
    assert len(patterns) == (28 if board.paired else 14)
    assignments = {item["pattern"]: item["netclass"] for item in patterns}
    assert len(assignments) == len(patterns)
    assert "GND" not in assignments
    for circuit in board.circuits:
        expected_class = prefix + (" CPWG" if circuit.coplanar else " noncoplanar")
        assert all(assignments[net] == expected_class for net in circuit.net_names)
        assert classes[expected_class]["priority"] < classes["Default"]["priority"]
        if board.paired:
            assert classes[expected_class]["diff_pair_gap"] == 0.2032


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_class_fixture_helpers_describe_one_intent_with_per_layer_references(
    case: RFCase,
) -> None:
    """Class-mode previews cover whole-route intent without width selectors."""
    specification = case.netclass_specification()
    assert specification.net_class == _expected_class(case)
    assert specification.excluded_layers == ()
    assert specification.target_ohms == ("90" if case.paired else "50")
    assert specification.kind == case.kind
    by_layer = {settings.layer: settings for settings in specification.layer_settings}
    assert len(by_layer) == len(specification.layer_settings)
    assert set(by_layer) == {trace.layer for trace in case.traces()}
    for profile in case.signal_profiles:
        settings = by_layer[profile.layer]
        assert settings.reference_layers == profile.reference_layers
        assert settings.spacing_nm == profile.spacing_nm
        assert settings.ground_gap_nm == profile.ground_gap_nm
    configuration = Config(specifications=(specification,))
    assert Config.from_dict(configuration.to_dict()) == configuration

    snapshot = case.snapshot()
    assert snapshot.layers == COPPER_LAYERS
    assert snapshot.traces == case.traces()
    assert snapshot.net_classes == (_expected_class(case),)
    assert snapshot.net_class_memberships == tuple(
        (net, (_expected_class(case),)) for net in case.net_names
    )


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_class_intent_finds_every_physical_route_profile_including_usb_launches(
    case: RFCase,
) -> None:
    """A single class captures short/long routes and every layer-changing width."""
    snapshot = case.snapshot()
    analysis = analyze(
        Config(specifications=(case.netclass_specification(),)), snapshot
    )
    assert len(analysis.sections) == len(case.signal_profiles)
    assert all(
        set(section.net_names) == set(case.net_names) for section in analysis.sections
    )
    captured_ids = [
        trace.trace_id for section in analysis.sections for trace in section.traces
    ]
    assert len(captured_ids) == len(set(captured_ids))
    assert set(captured_ids) == {trace.trace_id for trace in case.traces()}
    assert {(section.layer, section.width_nm) for section in analysis.sections} == {
        (trace.layer, trace.width_nm) for trace in case.traces()
    }


@pytest.mark.native_kicad
@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_native_kicad_class_membership_selects_exactly_the_rf_route(
    case: RFCase, tmp_path: Path, rf_examples: Path
) -> None:
    """Ask native DRC to evaluate membership on actual tracks, including inner widths."""
    executable = os.environ.get("KICAD_CLI") or shutil.which("kicad-cli")
    if executable is None:
        pytest.skip("Set KICAD_CLI to verify native net-class fixture membership")
    source_board = rf_examples / f"{case.name}.kicad_pcb"
    source_project = source_board.with_suffix(".kicad_pro")
    original_board = source_board.read_bytes()
    original_project = source_project.read_bytes()
    board = tmp_path / source_board.name
    shutil.copy2(source_board, board)
    shutil.copy2(source_project, board.with_suffix(".kicad_pro"))
    # An impossible minimum is a membership probe, not a fixture design rule.
    # It applies to tracks only so via/zone records cannot mask missing nets.
    board.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "RF net-class membership probe"\n'
        f"  (condition \"A.Type == 'Track' && A.hasNetclass('{_expected_class(case)}')\")\n"
        "  (constraint track_width (min 10mm)))\n",
        encoding="utf-8",
    )
    report_path = tmp_path / "membership-drc.json"
    result = subprocess.run(
        [
            executable,
            "pcb",
            "drc",
            "--format",
            "json",
            "--output",
            str(report_path),
            str(board),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    violations = [
        item for item in report["violations"] if item["type"] == "track_width"
    ]
    assert all(
        "RF net-class membership probe" in item["description"] for item in violations
    )
    reported_tracks = {
        item["uuid"] for violation in violations for item in violation["items"]
    }
    assert reported_tracks == {trace.trace_id for trace in case.traces()}
    assert source_board.read_bytes() == original_board
    assert source_project.read_bytes() == original_project
