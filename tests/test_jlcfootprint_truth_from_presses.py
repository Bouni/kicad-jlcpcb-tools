"""Tests for tools/jlcfootprint/truth_from_presses.py: preview presses plus the uploaded CPL give JLC's angle."""

import pytest

from tools.jlcfootprint import truth_from_presses as converter


def test_presses_add_to_the_uploaded_angle(tmp_path):
    """Comments and notes are tolerated, blanks stay blank, and a reference not in the CPL stops."""
    cpl = tmp_path / "cpl.csv"
    cpl.write_text(
        "Designator,Val,Package,Mid X,Mid Y,Rotation,Layer\n"
        "U1,x,SOIC,1,1,270.0,top\nQ1,x,SOT,1,1,-90.0,top\nU7,x,SOT,1,1,0.0,top\n",
        encoding="utf-8",
    )
    presses = tmp_path / "presses.csv"
    presses.write_text(
        '### CCW turns pressed in the preview\nU1,0\nQ1,180,"band at the box"\nU7,,wrong pick\n',
        encoding="utf-8",
    )
    out = tmp_path / "truth.csv"
    assert converter.main([str(presses), str(cpl), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").splitlines() == [
        "reference,observed_rotation,note",
        "U1,270,",
        "Q1,90,band at the box",
        "U7,,wrong pick",
    ]
    presses.write_text("R9,0\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="not in the CPL: R9"):
        converter.main([str(presses), str(cpl), "--out", str(out)])


def test_the_output_file_must_be_named(tmp_path, capsys):
    """With no --out the run stops before writing, so no committed truth file is overwritten."""
    presses = tmp_path / "presses.csv"
    presses.write_text("U1,0\n", encoding="utf-8")
    cpl = tmp_path / "cpl.csv"
    cpl.write_text("Designator,Rotation\nU1,0\n", encoding="utf-8")
    with pytest.raises(SystemExit) as stopped:
        converter.main([str(presses), str(cpl)])
    assert stopped.value.code == 2
    assert "--out" in capsys.readouterr().err
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "cpl.csv",
        "presses.csv",
    ]
