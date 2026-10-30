"""Workbook spacing values preserve bounded geometry and independent verdicts."""

from dataclasses import replace

import pytest

from impedance import spacing_checks as checks
from impedance.model import BoardSnapshot, Trace
from impedance.spacing_checks import NmBounds, SpacingCheck


def _check() -> SpacingCheck:
    """Provide a complete constant-gap assessment for display-state variations."""
    return SpacingCheck(
        declared_spacing_nm=203_200,
        comparison_band_nm=1_000,
        verdict="match",
        coverage="complete",
        minimum_gap_nm=NmBounds(203_200, 203_200),
        maximum_gap_nm=NmBounds(203_200, 203_200),
        assessed_length_nm=2_000_000,
        unverified_length_nm=0,
        issues=(),
        reasons=(),
    )


def test_rows_without_spacing_assessment_have_four_empty_cells() -> None:
    """Single-ended rows receive no differential values or comparison status."""
    assert checks.spacing_workbook_cells(None) == ("", "", "", "")


@pytest.mark.parametrize(
    ("check", "expected"),
    [
        (_check(), ("0.2032 mm", "", "0.2032 mm", "Match")),
        (
            replace(
                _check(),
                verdict="deviation",
                minimum_gap_nm=NmBounds(250_000, 250_000),
                maximum_gap_nm=NmBounds(250_000, 250_000),
            ),
            ("0.25 mm", "!=", "0.2032 mm", "Deviation"),
        ),
        (
            replace(
                _check(),
                verdict="undetermined",
                coverage="none",
                minimum_gap_nm=None,
                maximum_gap_nm=None,
            ),
            ("Unverified", "", "0.2032 mm", "Unverified"),
        ),
        (
            replace(_check(), verdict="needs_target", declared_spacing_nm=None),
            ("0.2032 mm", "", "Needs target", "Needs target"),
        ),
        (
            replace(
                _check(),
                verdict="needs_target",
                coverage="none",
                declared_spacing_nm=None,
                minimum_gap_nm=None,
                maximum_gap_nm=None,
            ),
            ("Unverified", "", "Needs target", "Needs target"),
        ),
        (
            replace(
                _check(),
                verdict="not_applicable",
                coverage="none",
                minimum_gap_nm=None,
                maximum_gap_nm=None,
                assessed_length_nm=0,
                transition_length_nm=2_000_000,
            ),
            ("Terminal transitions", "", "0.2032 mm", "Terminal transitions"),
        ),
        (
            replace(
                _check(),
                verdict="deviation",
                coverage="partial",
                minimum_gap_nm=NmBounds(250_000, 250_000),
                maximum_gap_nm=NmBounds(250_000, 250_000),
                unverified_length_nm=1_000_000,
            ),
            ("0.25 mm", "!=", "0.2032 mm", "Deviation · partial"),
        ),
        (
            replace(
                _check(),
                verdict="undetermined",
                coverage="partial",
                unverified_length_nm=1_000_000,
            ),
            ("0.2032 mm", "", "0.2032 mm", "Unverified · partial"),
        ),
    ],
)
def test_spacing_cell_states(
    check: SpacingCheck, expected: tuple[str, str, str, str]
) -> None:
    """Only a definite deviation earns a marker, including partial evidence."""
    assert checks.spacing_workbook_cells(check) == expected


@pytest.mark.parametrize(
    ("minimum", "maximum", "expected"),
    [
        (
            NmBounds(203_199, 203_200),
            NmBounds(219_999, 220_001),
            "0.2032–0.22 mm",
        ),
        (
            NmBounds(203_199, 203_201),
            NmBounds(203_199, 203_201),
            "0.2032–0.2032 mm",
        ),
        (NmBounds(-25_000, -25_000), NmBounds(-25_000, -25_000), "-0.025 mm"),
        (NmBounds(-25_005, -25_005), NmBounds(-25_005, -25_005), "-0.02501 mm"),
        (NmBounds(-1, -1), NmBounds(-1, -1), "0 mm"),
        (NmBounds(-25_000, -24_999), NmBounds(0, 0), "-0.025–0 mm"),
        (NmBounds(0, 0), NmBounds(0, 0), "0 mm"),
    ],
)
def test_actual_spacing_rounds_outer_bounds_and_signed_gaps(
    minimum: NmBounds, maximum: NmBounds, expected: str
) -> None:
    """Rounded uncertain gaps retain range notation, including zero and overlap."""
    check = replace(_check(), minimum_gap_nm=minimum, maximum_gap_nm=maximum)
    assert checks.spacing_workbook_cells(check)[0] == expected


@pytest.mark.parametrize("missing", ["minimum_gap_nm", "maximum_gap_nm"])
def test_incomplete_extrema_cannot_invent_a_numeric_actual(missing: str) -> None:
    """One available extremum cannot describe the complete displayed gap range."""
    check = replace(_check(), verdict="undetermined", **{missing: None})
    assert checks.spacing_workbook_cells(check) == (
        "Unverified",
        "",
        "0.2032 mm",
        "Unverified",
    )


@pytest.mark.parametrize(
    ("spacing_nm", "expected"),
    [
        (203_201, "0.2032 mm"),
        (203_205, "0.20321 mm"),
        (203_209, "0.20321 mm"),
        (199_999, "0.2 mm"),
        (1, "0 mm"),
        (1_000_000, "1 mm"),
        (123_456_789_012, "123456.78901 mm"),
    ],
)
def test_spacing_cells_round_display_without_changing_measurements(
    spacing_nm: int, expected: str
) -> None:
    """Both spacing columns follow the nominal-width five-place display rule."""
    check = replace(
        _check(),
        declared_spacing_nm=spacing_nm,
        minimum_gap_nm=NmBounds(spacing_nm, spacing_nm),
        maximum_gap_nm=NmBounds(spacing_nm, spacing_nm),
    )
    assert checks.spacing_workbook_cells(check) == (
        expected,
        "",
        expected,
        "Match",
    )
    assert check.declared_spacing_nm == spacing_nm
    assert (
        check.minimum_gap_nm == check.maximum_gap_nm == NmBounds(spacing_nm, spacing_nm)
    )


def test_spacing_details_retain_exact_nanometre_bounds() -> None:
    """Compact workbook values do not reduce the precision of detailed evidence."""
    check = replace(
        _check(),
        declared_spacing_nm=203_201,
        minimum_gap_nm=NmBounds(203_199, 203_201),
        maximum_gap_nm=NmBounds(219_999, 220_001),
    )
    details = checks.spacing_check_details(check)
    assert "Declared: 0.203201 mm" in details
    assert "Minimum: 0.203199–0.203201 mm" in details
    assert "Maximum: 0.219999–0.220001 mm" in details


@pytest.mark.parametrize("delta_nm", [-1_001, -1_000, -999, 0, 999, 1_000, 1_001])
def test_spacing_marker_retains_the_inclusive_one_micrometre_band(
    delta_nm: int,
) -> None:
    """Measured geometry uses the spacing band rather than the width cue limit."""
    gap_nm = 203_200 + delta_nm
    snapshot = BoardSnapshot(
        ("F.Cu",),
        (
            Trace("p", "F.Cu", "P", 150_000, ((0, 0), (1_000_000, 0))),
            Trace(
                "n",
                "F.Cu",
                "N",
                150_000,
                ((0, 150_000 + gap_nm), (1_000_000, 150_000 + gap_nm)),
            ),
        ),
        differential_pairs=(("P", "N"),),
    )
    check = checks.compare_pair_spacing(
        checks.measure_pair_spacing(snapshot, ("P", "N"), "F.Cu"), 203_200
    )
    cells = checks.spacing_workbook_cells(check)
    expected_gap = (
        "0.2022 mm" if delta_nm < 0 else "0.2042 mm" if delta_nm > 0 else "0.2032 mm"
    )
    assert cells[0] == expected_gap
    assert cells[1] == ("!=" if abs(delta_nm) > 1_000 else "")
    assert cells[3] == ("Deviation" if abs(delta_nm) > 1_000 else "Match")
