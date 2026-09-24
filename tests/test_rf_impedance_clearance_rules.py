"""Pin the user-requested layer-specific noncoplanar GND-pour clearances."""

from decimal import Decimal
import json
from typing import Any

import pytest

from scripts import generate_rf_impedance_fixtures as generator
from tests.rf_impedance_combined import COMBINED_BOARDS
from tests.rf_impedance_fixtures import COPPER_LAYERS, RF_CASES, RFCase
from tests.test_rf_impedance_matrix import _children, _field, _native_tree


def _signal_widths(
    native: list[Any], net_names: tuple[str, ...]
) -> dict[str, set[Decimal]]:
    """Read actual native signal copper, including connector-launch segments."""
    nets = {item[1]: item[2] for item in _children(native, "net")}
    result: dict[str, set[Decimal]] = {}
    for segment in _children(native, "segment"):
        if nets[_field(segment, "net")[0]] in net_names:
            layer = str(_field(segment, "layer")[0])
            result.setdefault(layer, set()).add(Decimal(_field(segment, "width")[0]))
    assert result, "The board must contain the declared circuit's actual tracks"
    return result


def _rules_by_layer(source: str) -> dict[str, list[Any]]:
    """Require one unambiguous same-layer rule per routed physical profile."""
    root = _native_tree(f"(kicad_pcb {source})")
    assert _field(root, "version") == ["1"]
    rules = _children(root, "rule")
    by_layer = {str(_field(rule, "layer")[0]): rule for rule in rules}
    assert len(by_layer) == len(rules)
    return by_layer


def _assert_rules(
    rules: dict[str, list[Any]], widths: dict[str, set[Decimal]], net_class: str
) -> None:
    """Check explicit target types and three times the actual serialized widths."""
    assert set(rules) == set(widths)
    for layer, actual_widths in widths.items():
        assert len(actual_widths) == 1, (
            "A fixed layer rule must not hide differing widths"
        )
        rule = rules[layer]
        assert set(str(_field(rule, "condition")[0]).split(" && ")) == {
            "A.Type == 'Track'",
            f"A.hasNetclass('{net_class}')",
            "B.Type == 'Zone'",
            "B.NetName == 'GND'",
        }
        constraint = _children(rule, "constraint")
        assert len(constraint) == 1 and constraint[0][1] == "clearance"
        value = str(_field(constraint[0], "min")[0])
        assert value.endswith("mm")
        assert Decimal(value[:-2]) == 3 * next(iter(actual_widths))


@pytest.mark.parametrize("case", RF_CASES, ids=lambda case: case.name)
def test_noncoplanar_rules_use_real_netclass_and_three_track_widths(
    case: RFCase,
) -> None:
    """Rules apply to track/GND-zone pairs only, on each exact signal layer."""
    source = generator.custom_rules_text(case)
    if case.coplanar:
        assert source is None, "CPWG must retain its existing coplanar gap"
        return
    assert isinstance(source, str)
    widths = _signal_widths(_native_tree(case.board_text()), case.net_names)
    rules = _rules_by_layer(source)
    _assert_rules(rules, widths, case.net_class)
    for layer, actual_widths in widths.items():
        expected = {
            (False, "F.Cu"): Decimal("1.05"),
            (False, "In2.Cu"): Decimal("0.39"),
            (False, "B.Cu"): Decimal("1.05"),
            (True, "F.Cu"): Decimal("0.9"),
            (True, "In2.Cu"): Decimal("0.33"),
            (True, "B.Cu"): Decimal("0.9"),
        }[case.paired, layer]
        assert {3 * width for width in actual_widths} == {expected}
    assert case.keepouts() == (), "Fixed keepouts would hide rule-driven clearance"


def test_both_combined_boards_carry_rules_and_accurate_circuit_metadata() -> None:
    """Two human-facing boards preserve rule scope, pours, and launch dimensions."""
    files = generator.fixture_files()
    expected = {f"{board.name}.kicad_dru" for board in COMBINED_BOARDS}
    assert len(expected) == 2
    assert {name for name in files if name.endswith(".kicad_dru")} == expected
    records = {record["name"]: record for record in json.loads(files["manifest.json"])}
    assert set(records) == {board.name for board in COMBINED_BOARDS}
    references = {
        "F.Cu": ["In1.Cu"],
        "In2.Cu": ["In1.Cu", "In3.Cu"],
        "B.Cu": ["In4.Cu"],
    }
    for board in COMBINED_BOARDS:
        record = records[board.name]
        assert record["custom_rules"] == f"{board.name}.kicad_dru"
        assert record["board"] == f"{board.name}.kicad_pcb"
        assert record["project"] == f"{board.name}.kicad_pro"
        native = _native_tree(files[record["board"]])
        ground = [
            zone for zone in _children(native, "zone") if not _children(zone, "keepout")
        ]
        assert len(ground) == 6
        assert {_field(zone, "layer")[0] for zone in ground} == set(COPPER_LAYERS)
        assert all(_field(zone, "net_name") == ["GND"] for zone in ground)
        rules = _rules_by_layer(files[record["custom_rules"]])
        project = json.loads(files[record["project"]])
        assignments = {
            item["pattern"]: item["netclass"]
            for item in project["net_settings"]["netclass_patterns"]
        }
        circuits = {item["name"]: item for item in record["circuits"]}
        assert set(circuits) == {circuit.name for circuit in board.circuits}
        noncoplanar_nets: tuple[str, ...] = ()
        for circuit in board.circuits:
            details = circuits[circuit.name]
            widths = _signal_widths(native, circuit.net_names)
            assert all(
                assignments[net] == circuit.net_class for net in circuit.net_names
            )
            assert {
                (leg["signal_layer"], Decimal(str(leg["width_mm"])))
                for leg in details["legs"]
            } == {
                (layer, width) for layer, values in widths.items() for width in values
            }
            for leg in details["legs"]:
                assert leg["reference_layers"] == references[leg["signal_layer"]]
                assert leg["noncoplanar_ground_clearance_mm"] == (
                    None
                    if circuit.coplanar
                    else float(3 * Decimal(str(leg["width_mm"])))
                )
            if circuit.coplanar:
                assert generator.custom_rules_text(circuit) is None
            else:
                noncoplanar_nets += circuit.net_names
        noncoplanar_class = next(c.net_class for c in board.circuits if not c.coplanar)
        _assert_rules(
            rules, _signal_widths(native, noncoplanar_nets), noncoplanar_class
        )


def test_opt_in_isolated_matrix_retains_independent_rule_sidecars() -> None:
    """Automation can still request isolated fixtures without restoring review clutter."""
    files = generator.fixture_files(legacy_matrix=True)
    expected = {f"{case.name}.kicad_dru" for case in RF_CASES if not case.coplanar}
    assert len(expected) == 14
    assert {name for name in files if name.endswith(".kicad_dru")} == expected
    records = {record["name"]: record for record in json.loads(files["manifest.json"])}
    for case in RF_CASES:
        record = records[case.name]
        assert record["custom_rules"] == (
            None if case.coplanar else f"{case.name}.kicad_dru"
        )
        widths = _signal_widths(_native_tree(files[record["board"]]), case.net_names)
        assert {
            (leg["signal_layer"], Decimal(str(leg["width_mm"])))
            for leg in record["legs"]
        } == {(layer, width) for layer, values in widths.items() for width in values}
        for description in record["legs"]:
            assert description["noncoplanar_ground_clearance_mm"] == (
                None
                if case.coplanar
                else float(3 * Decimal(str(description["width_mm"])))
            )
