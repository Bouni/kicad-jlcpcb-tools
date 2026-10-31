"""The gate as a test: the two committed gate boards must reproduce JLC's observed rotations."""

import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOARDS = ROOT / "tests" / "fixtures" / "jlcfootprint" / "boards"
BOARD = BOARDS / "corner_case" / "corner_case.kicad_pcb"
TRUTH = ROOT / "tests" / "fixtures" / "jlcfootprint" / "truth.csv"
BOARD_FUNCTIONS_AND_MARKS = (
    BOARDS / "pin_functions_and_marks" / "pin_functions_and_marks.kicad_pcb"
)
TRUTH_FUNCTIONS_AND_MARKS = (
    ROOT / "tests" / "fixtures" / "jlcfootprint" / "truth_pin_functions_and_marks.csv"
)
FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda"
PRO_FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda_pro"


def load_validator():
    """Import tools/jlcfootprint/validate_board.py as a module."""
    spec = importlib.util.spec_from_file_location(
        "validate_board", ROOT / "tools" / "jlcfootprint" / "validate_board.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_corner_case_board_matches_jlc():
    """Every observed rotation is reproduced and the wrong picks derive nothing."""
    validator = load_validator()
    rows = validator.evaluate(BOARD, FIXTURES)
    truth = validator.load_truth(TRUTH)
    origins = validator.load_truth_origins(TRUTH)
    # compare() passes a part the truth leaves out, so coverage is pinned here.
    assert set(truth) == {row["reference"] for row in rows}
    assert sum(value is not None for value in origins.values()) == 45
    assert validator.compare(rows, truth, origins) == []
    by_reference = {row["reference"]: row["verdict"] for row in rows}
    assert by_reference["U7"].fit == "count"
    assert by_reference["R4"].fit == "fits_tight"
    assert by_reference["D9"].polarity_light == "yellow"


def test_corner_case_board_matches_jlc_from_the_pro_fixtures():
    """Read from the Pro host's recorded answers, the board reproduces the same truth."""
    validator = load_validator()
    rows = validator.evaluate(BOARD, FIXTURES, pro_fixtures=PRO_FIXTURES)
    truth = validator.load_truth(TRUTH)
    assert set(truth) == {row["reference"] for row in rows}
    assert validator.compare(rows, truth, validator.load_truth_origins(TRUTH)) == []
    by_reference = {row["reference"]: row["verdict"] for row in rows}
    assert all(row["verdict"] is not None for row in rows), "every part is recorded"
    assert by_reference["U7"].fit == "count"
    assert by_reference["R4"].fit == "fits_tight"
    assert by_reference["D9"].polarity_light == "yellow"
    classic = {
        row["reference"]: row["verdict"] for row in validator.evaluate(BOARD, FIXTURES)
    }
    assert {
        ref: (v.status if v.status != "yellow" else "green", v.rotation)
        for ref, v in by_reference.items()
    } == {
        ref: (v.status if v.status != "yellow" else "green", v.rotation)
        for ref, v in classic.items()
    }


def test_pin_functions_and_marks_board_matches_jlc_from_the_pro_fixtures():
    """The second gate board reproduces its truth: function pairing, the drawings' marks, the body caveat."""
    validator = load_validator()
    rows = validator.evaluate(
        BOARD_FUNCTIONS_AND_MARKS, FIXTURES, pro_fixtures=PRO_FIXTURES
    )
    truth = validator.load_truth(TRUTH_FUNCTIONS_AND_MARKS)
    assert len(truth) == 16  # C8 (C1969735) included, read from JLC's preview
    assert set(truth) == {row["reference"] for row in rows}
    assert (
        validator.compare(
            rows, truth, validator.load_truth_origins(TRUTH_FUNCTIONS_AND_MARKS)
        )
        == []
    )
    by_reference = {row["reference"]: row["verdict"] for row in rows}
    assert all(verdict is not None for verdict in by_reference.values())
    assert (
        by_reference["U1"].rotation == 270
        and "paired by pin function" in by_reference["U1"].note_text
    )
    assert "by elimination" in by_reference["U2"].note_text
    assert "pad counts differ" not in by_reference["U4"].note_text
    assert by_reference["U5"].fit == "numbering"
    assert {by_reference[ref].polarity_source for ref in ("C1", "C2", "C3", "C4")} == {
        "drawing"
    }
    assert {by_reference[ref].rotation for ref in ("C5", "C6", "C7")} == {180}
    assert by_reference["C8"].rotation == 0
    assert by_reference["C9"].body_excess_mm == pytest.approx(0.9, abs=0.01)
    assert by_reference["C10"].body_excess_mm is None
    assert by_reference["C11"].rotation == 0


@pytest.mark.parametrize(
    ("board", "pro"),
    [
        (BOARD, False),
        (BOARD, True),
        (BOARD_FUNCTIONS_AND_MARKS, True),
    ],
    ids=["corner_case_classic", "corner_case_pro", "pin_functions_and_marks_pro"],
)
def test_each_board_reproduces_its_committed_report(board, pro):
    """The validator's table, byte for byte, is the pre_upload_report.txt committed beside the board."""
    validator = load_validator()
    rows = validator.evaluate(
        board, FIXTURES, pro_fixtures=PRO_FIXTURES if pro else None
    )
    committed = (board.parent / "pre_upload_report.txt").read_text(encoding="utf-8")
    assert validator.format_rows(rows) + "\n" == committed


def test_corner_case_board_courtyards_match_the_library():
    """C3 (CP_Elec_6.3x7.7) reads its 9.4 x 7.1 mm courtyard from the board file."""
    from pathlib import Path

    from jlcfootprint.boardfile import parse_kicad_pcb

    board = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "jlcfootprint"
        / "boards"
        / "corner_case"
        / "corner_case.kicad_pcb"
    )
    by_reference = {fp.reference: fp for fp in parse_kicad_pcb(str(board))}
    assert by_reference["C3"].courtyard == (-4.7, -3.55, 4.7, 3.55)
    assert by_reference["Q5"].courtyard is not None


def test_the_board_generator_needs_an_output_file(monkeypatch, capsys):
    """make_corner_case_board stops without --out, before it loads a board, so no gate board is overwritten."""
    monkeypatch.setitem(sys.modules, "pcbnew", types.ModuleType("pcbnew"))
    spec = importlib.util.spec_from_file_location(
        "make_corner_case_board",
        ROOT / "tools" / "jlcfootprint" / "make_corner_case_board.py",
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    with pytest.raises(SystemExit) as stopped:
        generator.main([])
    assert stopped.value.code == 2
    assert "--out" in capsys.readouterr().err
