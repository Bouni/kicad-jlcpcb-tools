"""JLC's package origin: the pure function, the origin on real parts and the gate."""

import math

import pytest

from jlcfootprint.fit import Placement, align, package_origin, pair_by_name
from jlcfootprint.geometry import Pad, easyeda_pads_to_mm, pad_box_centre
from jlcfootprint.resolver import Verdict, resolve
from tools.jlcfootprint import validate_board as validator
from tools.jlcfootprint.kicad_library import footprints_available, library_pads

from .jlcfootprint_support import recorded
from .test_jlcfootprint_validate import board_file, footprint_text

TRUTH_HEADER = "reference,observed_rotation,origin_dx_mm,origin_dy_mm\n"


def placement(rotation_deg: int, offset_x: float, offset_y: float) -> Placement:
    """Return a solved placement with the given angle and translation."""
    return Placement(
        rotation_deg=rotation_deg,
        offset_x=offset_x,
        offset_y=offset_y,
        is_mirrored=False,
        is_underdetermined=False,
        residual=0.0,
        angular_rms=0.0,
        matched=2,
    )


def turned(rotation_deg: int, x: float, y: float) -> tuple:
    """Return ``(x, y)`` turned by the angle in the solver's Y-down math frame."""
    theta = math.radians(rotation_deg)
    cos, sin = math.cos(theta), math.sin(theta)
    return (x * cos - y * sin, x * sin + y * cos)


# ----------------------------------------------------------------------------
# The pure origin
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_the_origin_is_the_placement_run_backwards_from_the_drawing_origin(rotation):
    """At every snapped angle the origin is the point the placement maps onto (0, 0)."""
    solved = placement(rotation, 1.5, -0.25)

    origin = package_origin(solved)

    # Forward: jlc = R(theta) * origin + offset must land on the drawing's (0, 0).
    forward = turned(rotation, *origin)
    assert forward[0] + solved.offset_x == pytest.approx(0.0, abs=1e-12)
    assert forward[1] + solved.offset_y == pytest.approx(0.0, abs=1e-12)


def test_a_placement_with_no_translation_puts_the_origin_on_the_footprint_origin():
    """A drawing already centred on the footprint's origin derives (0, 0)."""
    assert package_origin(placement(90, 0.0, 0.0)) == (0.0, 0.0)


def test_a_verdict_without_a_placement_or_a_fit_has_no_origin():
    """Red and unknown verdicts carry no origin, whatever the placement said."""
    assert Verdict().origin is None
    unfitted = Verdict(status="red", placement=placement(0, 1.0, 1.0))
    assert unfitted.origin is None
    assert Verdict(status="unknown", placement=placement(0, 1.0, 1.0)).origin is None
    fitted = Verdict(status="yellow", placement=placement(0, 1.0, 1.0))
    assert fitted.origin == package_origin(fitted.placement)


# ----------------------------------------------------------------------------
# The origin on real parts
# ----------------------------------------------------------------------------


needs_footprints = pytest.mark.skipif(
    not footprints_available(), reason="KiCad library footprints unavailable"
)


@needs_footprints
def test_a_pin_one_origin_footprint_reads_its_origin_away_from_the_footprint_origin():
    """A DIP-8 numbered from pin 1 puts JLC's centred drawing a row and a half away."""
    pads = library_pads("Package_DIP", "DIP-8_W7.62mm")
    record = recorded("C46749")

    verdict = resolve(
        pads,
        "DIP-8_W7.62mm",
        record.status,
        record.package_name,
        easyeda_pads_to_mm(record.pads),
        record.symbol_pins,
    )

    assert verdict.status == "green"
    origin = verdict.origin
    assert origin is not None
    # KiCad's origin sits on pad 1; JLC's drawing is centred, which is 1.5 pitches
    # along the row and half the row spacing across it.
    assert origin[0] == pytest.approx(3.81, abs=0.005)
    assert origin[1] == pytest.approx(3.81, abs=0.005)
    assert pad_box_centre(pads) == pytest.approx(origin, abs=0.005)


@needs_footprints
def test_a_dpak_reads_its_origin_well_off_the_pad_box_centre():
    """TO-252-2 is the case the preview settles: JLC's origin is 0.72 mm from the box."""
    pads = library_pads("Package_TO_SOT_SMD", "TO-252-2")
    record = recorded("C58069")

    verdict = resolve(
        pads,
        "TO-252-2",
        record.status,
        record.package_name,
        easyeda_pads_to_mm(record.pads),
        record.symbol_pins,
    )

    origin = verdict.origin
    centre = pad_box_centre(pads)
    assert origin == pytest.approx((-1.565, 0.0), abs=0.005)
    assert origin[0] - centre[0] == pytest.approx(-0.725, abs=0.005)


def test_a_bottom_part_derives_the_same_origin_as_the_same_part_on_top(tmp_path):
    """The validator un-mirrors a bottom footprint, so both sides read one origin."""
    body = footprint_text("Q1", "F.Cu", 0) + footprint_text(
        "Q2", "B.Cu", 0, mirror=True
    )

    rows = {
        row["reference"]: row
        for row in validator.evaluate(
            board_file(tmp_path, body), validator.DEFAULT_FIXTURES
        )
    }

    top = rows["Q1"]["verdict"].origin
    bottom = rows["Q2"]["verdict"].origin
    assert top is not None
    assert bottom == pytest.approx(top, abs=1e-9)


def test_the_solver_and_the_origin_agree_on_a_hand_built_offset_drawing():
    """A drawing whose origin is 0.2 mm off the pads derives exactly that offset."""
    kicad = [Pad("1", -1.0, 0.0, 0.6, 0.6), Pad("2", 1.0, 0.0, 0.6, 0.6)]
    # The same two pads, drawn 0.2 mm to the left of JLC's own origin.
    jlc = [Pad("1", -1.2, 0.0, 0.6, 0.6), Pad("2", 0.8, 0.0, 0.6, 0.6)]

    matched_k, matched_j, _ = pair_by_name(kicad, jlc)
    origin = package_origin(align(matched_k, matched_j))

    assert origin == pytest.approx((0.2, 0.0), abs=1e-9)


# ----------------------------------------------------------------------------
# The gate
# ----------------------------------------------------------------------------


def truth_file(tmp_path, body: str, header: str = TRUTH_HEADER):
    """Write a truth CSV and return its path."""
    path = tmp_path / "truth.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def sot23_rows(validator, tmp_path):
    """Return the validator's rows for one SOT-23 on the top side."""
    body = footprint_text("Q1", "F.Cu", 0)
    return validator.evaluate(board_file(tmp_path, body), validator.DEFAULT_FIXTURES)


def test_the_report_prints_the_origin_offset_from_the_pad_box_centre(tmp_path):
    """The two origin columns say how far the CPL position would move, to two decimals."""
    rows = sot23_rows(validator, tmp_path)

    table = validator.format_rows(rows)

    assert "orgdx  orgdy" in table.splitlines()[0]
    # C2132's EasyEDA drawing sits 0.099 mm right of the pad box KiCad draws.
    assert validator.origin_offset(rows[0])[0] == pytest.approx(0.099, abs=0.001)
    assert "  0.10   0.00" in table.splitlines()[1]
    # It is the offset, not the origin: a footprint whose pad box is not on its own
    # origin (a connector numbered from pin 1) shows the two apart.
    moved = dict(rows[0])
    moved["centre"] = (0.5, -0.25)
    assert validator.origin_offset(moved) == pytest.approx(
        (0.099 - 0.5, 0.25), abs=0.001
    )


def test_the_report_leaves_the_origin_columns_blank_without_a_verdict(tmp_path):
    """A part with no recorded response, and a refusal, print no origin."""
    rows = sot23_rows(validator, tmp_path)
    rows[0]["verdict"] = None

    assert validator.origin_offset(rows[0]) is None
    assert (
        validator.format_rows(rows)
        .splitlines()[1]
        .endswith("Package_TO_SOT_SMD:SOT-23")
    )


def test_truth_origins_are_optional_and_blank_means_no_origin(tmp_path):
    """A truth file without the origin columns gates no origin; an empty pair expects none."""
    without = truth_file(tmp_path, "Q1,180\n", header="reference,observed_rotation\n")
    assert validator.load_truth_origins(without) is None

    with_columns = truth_file(tmp_path, "Q1,180,0.099,0\nQ2,180,,\n")
    assert validator.load_truth_origins(with_columns) == {
        "Q1": (0.099, 0.0),
        "Q2": None,
    }


def test_a_half_filled_truth_origin_is_rejected(tmp_path):
    """One of the two numbers missing is a mistake in the file, not a blank row."""
    path = truth_file(tmp_path, "Q1,180,0.099,\n")

    with pytest.raises(SystemExit, match="non-numeric origin"):
        validator.load_truth_origins(path)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_a_non_finite_truth_origin_is_rejected(tmp_path, value):
    """A NaN would compare as within tolerance and switch that origin check off."""
    path = truth_file(tmp_path, f"Q1,180,{value},0\n")

    with pytest.raises(SystemExit, match="non-finite origin"):
        validator.load_truth_origins(path)


def test_a_half_present_origin_header_is_rejected(tmp_path):
    """A misspelt origin column would otherwise turn every origin check off."""
    header = "reference,observed_rotation,origin_dx_mm,origin_dy\n"
    path = truth_file(tmp_path, "Q1,180,0.099,0\n", header=header)

    with pytest.raises(SystemExit, match="only origin_dx_mm is there"):
        validator.load_truth_origins(path)


@pytest.mark.parametrize(
    ("jlc", "rotation"),
    [
        ([Pad("1", 2, 0, 0.8, 0.9), Pad("2", 0, 0, 0.8, 0.9)], 0),
        ([Pad("1", 0, 2, 0.8, 0.9, 90), Pad("2", 0, 0, 0.8, 0.9, 90)], 90),
    ],
)
def test_an_axis_aligned_part_reports_the_origin_of_the_rotation_it_emits(
    jlc, rotation
):
    """JLC's pad at its origin must land on a KiCad pad at the emitted rotation, not at 180 more.

    The pairing by number solves at 180 or 270 here; the axis rotation is folded under
    180, and the origin must come from the pairing that gives the folded turn.
    """
    kicad = [Pad("1", -1, 0, 0.8, 0.9), Pad("2", 1, 0, 0.8, 0.9)]
    verdict = resolve(kicad, "Resistor_SMD:R_0603_1608Metric", "ok", "R0603", jlc, [])
    assert (verdict.status, verdict.method, verdict.rotation) == (
        "green",
        "axis",
        rotation,
    )
    # JLC's pad 2 is drawn at JLC's origin, so wherever the turn takes the rest of
    # the drawing, the origin itself is where that pad lands: on KiCad's pad 1, the
    # other KiCad pad being where pad 1's drawing at (2, 0) or (0, 2) turns to.
    assert verdict.origin == pytest.approx((-1.0, 0.0), abs=1e-9)


@pytest.mark.parametrize(
    ("dx", "reasons"),
    [(0.099, 0), (0.103, 0), (0.12, 1)],
)
def test_a_truth_origin_is_compared_within_five_microns(tmp_path, dx, reasons):
    """Five microns of slack absorbs the drawing's rounding and nothing more."""
    rows = sot23_rows(validator, tmp_path)

    failures = validator.compare(rows, {"Q1": "180"}, {"Q1": (dx, 0.0)})

    assert len(failures) == reasons
    assert not failures or "differs from truth" in failures[0][1]


def test_a_truth_row_expecting_no_origin_fails_when_one_is_derived(tmp_path):
    """A blank pair is a claim, so a part that does derive an origin is a disagreement."""
    rows = sot23_rows(validator, tmp_path)

    failures = validator.compare(rows, {"Q1": "180"}, {"Q1": None})

    assert len(failures) == 1
    assert "expected no derived origin" in failures[0][1]


def test_a_truth_row_with_an_origin_fails_when_the_part_refuses(tmp_path):
    """A refusal carries no origin, so a filled truth row must fail rather than pass."""
    rows = sot23_rows(validator, tmp_path)
    rows[0]["verdict"] = Verdict(status="red")

    failures = validator.compare(rows, {"Q1": ""}, {"Q1": (0.099, 0.0)})

    assert len(failures) == 1
    assert "no derived origin" in failures[0][1]
