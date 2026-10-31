"""Tests for tools/jlcfootprint/validate_board.py: the gate's evaluate and compare plumbing."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fabrication_test_support import (
    Point,
    library as fabrication_library,
    make_fabrication,
    make_footprint,
    modules as fabrication_modules,
)
from tools.jlcfootprint import validate_board as validator

modules = fabrication_modules
library = fabrication_library

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda"
PRO_FIXTURES = ROOT / "tests" / "fixtures" / "jlcfootprint" / "easyeda_pro"


def footprint_text(reference, layer, placed, mirror=False):
    """Return one SOT-23 footprint (C2132) as KiCad stores it: absolute pad angles, mirrored Y on the back."""
    sign = -1 if mirror else 1
    at = f"(at 10 10 {placed})" if placed else "(at 10 10)"
    rot = f" {placed}" if placed else ""
    pads = [
        ("1", -0.9375, -0.95 * sign),
        ("2", -0.9375, 0.95 * sign),
        ("3", 0.9375, 0.0),
    ]
    pad_lines = "".join(
        f'    (pad "{number}" smd roundrect (at {x} {y}{rot}) (size 1.475 0.6) (layers "{layer}"))\n'
        for number, x, y in pads
    )
    return (
        f'  (footprint "Package_TO_SOT_SMD:SOT-23" (layer "{layer}") {at}\n'
        f'    (property "Reference" "{reference}" (at 0 0) (layer "F.SilkS"))\n'
        f'    (property "LCSC" "C2132" (at 0 0) (layer "F.Fab"))\n'
        f"{pad_lines}  )\n"
    )


def board_file(tmp_path, body):
    """Write a minimal board around ``body`` and return its path."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(
        f'(kicad_pcb (version 20240108) (generator "pcbnew")\n{body})\n',
        encoding="utf-8",
    )
    return path


def test_sot23_at_every_placement_emits_the_same_correction(tmp_path):
    """Four top placements and two bottom ones all derive 180, and the CPL check agrees with itself."""
    body = "".join(
        footprint_text(f"Q{i}", "F.Cu", r) for i, r in enumerate((0, 90, 180, 270), 1)
    )
    body += footprint_text("Q5", "B.Cu", 0, mirror=True)
    body += footprint_text("Q6", "B.Cu", 90, mirror=True)
    rows = validator.evaluate(board_file(tmp_path, body), FIXTURES)
    assert [row["verdict"].rotation for row in rows] == [180] * 6
    truth = {
        row["reference"]: str(
            validator.expected_cpl_rotation(row["placed"], row["bottom"], 180)
        )
        for row in rows
    }
    assert validator.compare(rows, truth) == []
    wrong = dict(truth, Q2="0")
    assert [reason for _, reason in validator.compare(rows, wrong)] == [
        "would emit 270, JLC shows 0"
    ]


@pytest.mark.parametrize("correction", [0, 90, 180, 270])
@pytest.mark.parametrize("bottom", [False, True], ids=["top", "bottom"])
@pytest.mark.parametrize("placed", [0, 45, 90, 180, 270])
def test_expected_cpl_rotation_matches_upstream(
    modules, library, tmp_path, placed, bottom, correction
):
    """The gate's CPL angle is the one Fabrication writes: a bottom part mirrored as 180 minus the angle, the correction added after."""
    fabrication = make_fabrication(modules, library, tmp_path)
    footprint = make_footprint("U1", 31 if bottom else 0, placed, Point(0, 0))
    match = SimpleNamespace(correction=SimpleNamespace(rotation=correction))
    assert validator.expected_cpl_rotation(
        placed, bottom, correction
    ) == fabrication._rotation_for_match(footprint, match)


def test_missing_fixture_and_blank_truth(tmp_path):
    """No recorded response is reported; a blank truth means 'must not derive'."""
    body = footprint_text("Q1", "F.Cu", 0).replace("C2132", "C999999999")
    rows = validator.evaluate(board_file(tmp_path, body), FIXTURES)
    assert rows[0]["verdict"] is None
    assert validator.compare(rows, {"Q1": "90"})[0][1] == "no recorded EasyEDA response"
    assert validator.compare(rows, {}) == []
    typo = validator.compare(rows, {"Q1": "90", "Q99": "0"})
    assert [(row["reference"], reason) for row, reason in typo] == [
        ("Q99", "not on the board (typo in truth.csv?)"),
        ("Q1", "no recorded EasyEDA response"),
    ]
    good = validator.evaluate(
        board_file(tmp_path, footprint_text("Q1", "F.Cu", 0)), FIXTURES
    )
    assert (
        validator.compare(good, {"Q1": ""})[0][1]
        == "expected no derived rotation, got 180"
    )


def test_rows_come_out_in_natural_reference_order(tmp_path):
    """Q2 sorts before Q10, whatever order pcbnew wrote the footprints in."""
    body = "".join(footprint_text(ref, "F.Cu", 0) for ref in ("Q10", "Q2", "Q1"))
    rows = validator.evaluate(board_file(tmp_path, body), FIXTURES)
    assert [row["reference"] for row in rows] == ["Q1", "Q2", "Q10"]
    assert validator.reference_key("LED3") < validator.reference_key("Q1")


def test_main_end_to_end_reports_and_exits(tmp_path, capsys):
    """The gate is main's exit code: 0 when JLC agrees, 1 with one reason line per disagreement."""
    body = footprint_text("Q1", "F.Cu", 0) + footprint_text(
        "Q2", "B.Cu", 90, mirror=True
    )
    board = board_file(tmp_path, body)
    truth = tmp_path / "truth.csv"
    report = tmp_path / "report.txt"
    truth.write_text("reference,observed_rotation\nQ1,180\nQ2,270\n", encoding="utf-8")
    argv = [str(board), "--fixtures", str(FIXTURES), "--truth", str(truth)]
    assert validator.main([*argv, "--report", str(report)]) == 0
    out = capsys.readouterr().out
    assert "2 parts checked against JLC, 0 disagree" in out
    assert (
        report.read_text(encoding="utf-8")
        == validator.format_rows(validator.evaluate(board, FIXTURES)) + "\n"
    )
    truth.write_text("reference,observed_rotation\nQ1,180\nQ2,0\n", encoding="utf-8")
    assert validator.main(argv) == 1
    out = capsys.readouterr().out
    assert "2 parts checked against JLC, 1 disagree" in out
    assert "Q2" in out and "placed 90 bottom: would emit 270, JLC shows 0" in out


def test_load_truth_tolerates_spreadsheet_output(tmp_path):
    """A BOM, capitalised headers and a missing cell are read; a missing column stops the run."""
    path = tmp_path / "truth.csv"
    path.write_text("﻿Reference,Observed_Rotation\nQ1,90\nQ2\n\n", encoding="utf-8")
    assert validator.load_truth(path) == {"Q1": "90", "Q2": ""}
    path.write_text("ref,observed_rotation\nQ1,90\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="no 'reference' column"):
        validator.load_truth(path)


def test_load_truth_refuses_a_reference_named_twice(tmp_path):
    """A second row for a part would replace the first unseen, so it stops the run."""
    path = tmp_path / "truth.csv"
    path.write_text("reference,observed_rotation\nU1,0\nU1,270\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="U1 has more than one row"):
        validator.load_truth(path)


def test_refused_part_with_a_recorded_angle_is_a_disagreement(tmp_path):
    """A red verdict against a JLC angle is reported with the status, never silently skipped."""
    body = footprint_text("U1", "F.Cu", 0).replace("C2132", "C3014306")
    rows = validator.evaluate(board_file(tmp_path, body), FIXTURES)
    assert rows[0]["verdict"].status == "red"
    assert validator.compare(rows, {"U1": "180"})[0][1] == (
        "no derived rotation (red); JLC shows 180"
    )
    good = validator.evaluate(
        board_file(tmp_path, footprint_text("Q1", "F.Cu", 0)), FIXTURES
    )
    assert validator.compare(good, {"Q1": "180 deg"})[0][1] == (
        "truth value '180 deg' is not a number"
    )


def test_evaluate_uses_the_plugin_flip_constant_by_default(tmp_path, monkeypatch):
    """With no --flip-y the gate tests FLIP_EASYEDA_Y itself, so changing the constant changes the gate."""
    board = board_file(tmp_path, footprint_text("Q1", "F.Cu", 0))
    as_shipped = validator.evaluate(board, FIXTURES)[0]["verdict"]
    geometry = importlib.import_module("jlcfootprint.geometry")
    monkeypatch.setattr(geometry, "FLIP_EASYEDA_Y", not geometry.FLIP_EASYEDA_Y)
    flipped = validator.evaluate(board, FIXTURES)[0]["verdict"]
    assert (as_shipped.rotation, as_shipped.status) != (
        flipped.rotation,
        flipped.status,
    )
    assert validator.evaluate(board, FIXTURES, flip_y=False)[0]["verdict"].rotation == (
        as_shipped.rotation
    )


def test_corrupt_fixture_names_the_file(tmp_path):
    """A truncated recording stops the run with its path instead of a bare JSON error."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "C2132.json").write_text('{"success": tru', encoding="utf-8")
    board = board_file(tmp_path, footprint_text("Q1", "F.Cu", 0))
    with pytest.raises(SystemExit, match="C2132.json: not valid JSON"):
        validator.evaluate(board, fixtures)


def test_axis_parts_compare_modulo_180():
    """A resistor JLC shows at 180 where we emit 0 is the same placement; polarized and shape-aligned parts are not."""

    def row(reference, method, rotation):
        return {
            "reference": reference,
            "lcsc": "C1",
            "footprint": "F",
            "placed": 0.0,
            "bottom": False,
            "package": "",
            "verdict": validator.Verdict(
                status="green",
                rotation=rotation,
                method=method,
                non_polar=method in ("axis", "shape"),
            ),
        }

    rows = [row("R1", "axis", 0), row("D1", "polarity", 0), row("BT1", "shape", 180)]
    assert validator.compare(rows, {"R1": "180", "D1": "0", "BT1": "180"}) == []
    assert [
        r["reference"]
        for r, _ in validator.compare(rows, {"R1": "90", "D1": "180", "BT1": "0"})
    ] == [
        "R1",
        "D1",
        "BT1",
    ]


def test_angles_compare_within_a_tolerance_across_the_wrap():
    """A bottom part at 179.9 degrees emits 0.1 give or take float noise; 359.9995 is 0."""
    row = {
        "reference": "D1",
        "lcsc": "C1",
        "footprint": "F",
        "placed": 179.9,
        "bottom": True,
        "package": "",
        "verdict": validator.Verdict(status="green", rotation=0, method="polarity"),
    }
    assert validator.compare([row], {"D1": "0.1"}) == []
    assert validator.compare([dict(row, placed=180.0)], {"D1": "359.9995"}) == []
    assert (
        validator.compare([row], {"D1": "0.2"})[0][1] == "would emit 0.1, JLC shows 0.2"
    )


def test_a_non_finite_truth_angle_is_a_disagreement():
    """float() reads nan and inf, which would otherwise pass every tolerance comparison."""
    row = {
        "reference": "D1",
        "lcsc": "C1",
        "footprint": "F",
        "placed": 0.0,
        "bottom": False,
        "package": "",
        "verdict": validator.Verdict(status="green", rotation=0, method="polarity"),
    }
    for value in ("nan", "inf", "-inf"):
        assert validator.compare([row], {"D1": value}) == [
            (row, f"truth value {value!r} is not a number")
        ]


def test_pro_fixtures_assemble_a_part_like_the_cache_would(tmp_path):
    """The batch answer gives the uuids, the documents the pads and pins; gaps read as unrecorded."""
    import json
    import shutil

    index = validator.load_pro_index(PRO_FIXTURES)
    assert index["C2132"] is not None and len(index) >= 35
    record = validator.load_pro_record(PRO_FIXTURES, index, "C2132")
    assert (record.status, record.package_name) == (
        "ok",
        "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR",
    )
    assert sorted(p["number"] for p in record.pads) == ["1", "2", "3"]
    assert sorted((p.number, p.label) for p in record.symbol_pins) == [
        ("1", "B"),
        ("2", "E"),
        ("3", "C"),
    ]
    assert record.footprint_source == "puuid-endpoint"
    assert validator.load_pro_record(PRO_FIXTURES, index, "C999999") is None
    # A miss the batch was asked about is the checkerboard case; a partial recording is unrecorded.
    devices = json.loads((PRO_FIXTURES / "devices_corner_case.json").read_text())
    partial = tmp_path / "pro"
    partial.mkdir()
    (partial / "devices_x.json").write_text(
        json.dumps({"codes": devices["codes"] + ["C404"], "body": devices["body"]})
    )
    index = validator.load_pro_index(partial)
    assert validator.load_pro_record(partial, index, "C404").status == "none"
    assert validator.load_pro_record(partial, index, "C2132") is None
    hit = index["C2132"]
    shutil.copy(PRO_FIXTURES / f"footprint_{hit.puuid}.json", partial)
    assert validator.load_pro_record(partial, index, "C2132") is None, "symbol missing"
    shutil.copy(PRO_FIXTURES / f"symbol_{hit.symbol_uuid}.json", partial)
    assert validator.load_pro_record(partial, index, "C2132").status == "ok"
    rows = validator.evaluate(
        ROOT / "tests/fixtures/jlcfootprint/boards/corner_case/corner_case.kicad_pcb",
        FIXTURES,
        pro_fixtures=partial,
    )
    by_reference = {row["reference"]: row for row in rows}
    assert by_reference["Q1"]["verdict"].rotation == 180
    assert by_reference["U1"]["verdict"] is None


def test_a_recorded_footprint_keeps_its_own_status(tmp_path):
    """A footprint document that cannot be read is an error to retry, one that answered nothing is none."""
    import json
    import shutil

    pro = tmp_path / "pro"
    shutil.copytree(PRO_FIXTURES, pro)
    index = validator.load_pro_index(pro)
    footprint = pro / f"footprint_{index['C7950'].puuid}.json"
    footprint.write_text(
        json.dumps(
            {
                "success": True,
                "code": 0,
                "result": {"dataStr": '["DOCTYPE","FOOTPRINT","1.8"]', "title": "x"},
            }
        )
    )
    assert validator.load_pro_record(pro, index, "C7950").status == "error"
    rows = validator.evaluate(
        ROOT / "tests/fixtures/jlcfootprint/boards/corner_case/corner_case.kicad_pcb",
        FIXTURES,
        pro_fixtures=pro,
    )
    soic = [row["verdict"] for row in rows if row["lcsc"] == "C7950"]
    assert soic and all(verdict.status == "unknown" for verdict in soic)
    assert all("will retry" in verdict.note_text for verdict in soic)
    footprint.write_text(json.dumps({"success": False, "code": 404, "message": "x"}))
    assert validator.load_pro_record(pro, index, "C7950").status == "none"


def test_pro_fixtures_reject_a_broken_batch_file(tmp_path):
    """A batch file that is not a recorded answer stops the run with its name."""
    broken = tmp_path / "pro"
    broken.mkdir()
    (broken / "devices_bad.json").write_text("[]")
    with pytest.raises(SystemExit, match="devices_bad.json"):
        validator.load_pro_index(broken)
    (broken / "devices_bad.json").write_text(
        '{"codes": ["C1"], "body": {"success": false, "code": 401, "message": "denied"}}'
    )
    with pytest.raises(SystemExit, match="401"):
        validator.load_pro_index(broken)
