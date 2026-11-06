"""Tests for jlcfootprint.resolver: classification and the multi-pin alignment path."""

import json
from pathlib import Path

from jlcfootprint.easyeda_parse import SymbolPin, parse_component_response
from jlcfootprint.geometry import Pad, easyeda_pads_to_mm
from jlcfootprint.polarity import (
    DIODE,
    OTHER,
    POLAR_CAP,
    normalise_function,
    part_kind,
    terminal_of,
)
from jlcfootprint.resolver import resolve

FIXTURES = Path(__file__).parent / "fixtures" / "jlcfootprint" / "easyeda"

# KiCad library pads, footprint frame, Y-down (Package_TO_SOT_SMD:SOT-23).
KICAD_SOT23 = [
    Pad("1", -0.9375, -0.95, 1.475, 0.6),
    Pad("2", -0.9375, 0.95, 1.475, 0.6),
    Pad("3", 0.9375, 0.0, 1.475, 0.6),
]

# KiCad library pads (Package_SO:SOIC-8_3.9x4.9mm_P1.27mm): pin 1 top-left, 1-4 down the left.
KICAD_SOIC8 = [
    Pad("1", -2.475, -1.905, 1.95, 0.6),
    Pad("2", -2.475, -0.635, 1.95, 0.6),
    Pad("3", -2.475, 0.635, 1.95, 0.6),
    Pad("4", -2.475, 1.905, 1.95, 0.6),
    Pad("5", 2.475, 1.905, 1.95, 0.6),
    Pad("6", 2.475, 0.635, 1.95, 0.6),
    Pad("7", 2.475, -0.635, 1.95, 0.6),
    Pad("8", 2.475, -1.905, 1.95, 0.6),
]


def rotated_on_screen_ccw(pads, degrees):
    """Rotate a pad pattern counter-clockwise as seen on screen (Y-down frame)."""
    steps = (degrees // 90) % 4
    out = pads
    for _ in range(steps):
        out = [p._replace(x=p.y, y=-p.x, rotation=(p.rotation + 90) % 360) for p in out]
    return out


def load_jlc(lcsc):
    """Return (package_name, jlc pads in mm, symbol pins) from a recorded response."""
    record = parse_component_response(
        json.loads((FIXTURES / f"{lcsc}.json").read_text()), lcsc
    )
    return record.package_name, easyeda_pads_to_mm(record.pads), record.symbol_pins


def test_normalise_function_and_terminal_of():
    """Pin functions such as K_1 or anode reduce to a terminal name; +/- stay literal."""
    assert normalise_function("K_1") == "K"
    assert normalise_function("anode") == "ANODE"
    assert normalise_function("+") == "+"
    assert normalise_function("~") == ""
    assert terminal_of(Pad("1", 0, 0, 1, 1, 0, "K_1")) == "cathode"
    assert terminal_of(Pad("2", 0, 0, 1, 1, 0, "A")) == "anode"
    assert terminal_of(Pad("1", 0, 0, 1, 1, 0, "+")) == "positive"
    assert terminal_of(Pad("1", 0, 0, 1, 1, 0, "")) == ""


def test_part_kind_from_family_footprint_functions_or_labels():
    """Any side may declare the part polarized: KiCad evidence, then the family, then labels."""
    plain = [Pad("1", 0, 0, 1, 1), Pad("2", 1, 0, 1, 1)]
    wired = [Pad("1", 0, 0, 1, 1, 0, "K"), Pad("2", 1, 0, 1, 1, 0, "A")]
    assert part_kind("R0603", "Resistor_SMD:R_0603_1608Metric", plain, []) is OTHER
    assert part_kind("SMF_L2.8-W1.8-LS3.7-RD", "Diode_SMD:D_SMF", plain, []) is DIODE
    assert part_kind("DIO-SMD_X", "Custom:Thing", wired, []) is DIODE
    assert part_kind("X", "LED_SMD:LED_0603_1608Metric", plain, []) is DIODE
    assert (
        part_kind(
            "X", "Custom:Thing", plain, [SymbolPin("1", "A"), SymbolPin("2", "K")]
        )
        is DIODE
    )
    assert (
        part_kind(
            "CAP-SMD_BD6.3-L6.6-W6.6-FD", "Capacitor_SMD:CP_Elec_6.3x7.7", plain, []
        )
        is POLAR_CAP
    )
    assert (
        part_kind("CAP-SMD_BD6.3-L6.6-W6.6-RD", "Custom:Elec", plain, []) is POLAR_CAP
    )
    assert (
        part_kind(
            "X", "Custom:Thing", plain, [SymbolPin("1", "+"), SymbolPin("2", "-")]
        )
        is POLAR_CAP
    )


def test_part_kind_reads_kicad_diode_evidence_before_capacitor_evidence():
    """An LED whose pads say +/- is a diode: the footprint name and K/A functions come first."""
    plus_minus = [Pad("1", 0, 0, 1, 1, 0, "-"), Pad("2", 1, 0, 1, 1, 0, "+")]
    assert (
        part_kind("LED0805-RD", "LED_SMD:LED_0805_2012Metric", plus_minus, []) is DIODE
    )
    assert (
        part_kind("SMF_L2.8-W1.8-LS3.7-RD", "Diode_SMD:D_SMF", plus_minus, []) is DIODE
    )
    wired = [Pad("1", 0, 0, 1, 1, 0, "K"), Pad("2", 1, 0, 1, 1, 0, "A")]
    cap_labels = [SymbolPin("1", "+"), SymbolPin("2", "-")]
    assert (
        part_kind("CAP-SMD_BD6.3-L6.6-W6.6-FD", "Custom:Thing", wired, cap_labels)
        is DIODE
    )
    assert (
        part_kind("CAP-SMD_BD6.3-L6.6-W6.6-FD", "Custom:Thing", plus_minus, [])
        is POLAR_CAP
    )


def test_part_kind_takes_a_diode_only_from_evidence_naming_its_cathode():
    """A connector's A1/A2, a switch's A/B and a transistor's C/E name no diode; A with K or C does."""
    plain = [Pad("1", 0, 0, 1, 1), Pad("2", 1, 0, 1, 1)]
    connector = [Pad("1", 0, 0, 1, 1, 0, "A1"), Pad("2", 1, 0, 1, 1, 0, "A2")]
    assert part_kind("CONN-SMD_2P", "Custom:Conn", connector, []) is OTHER

    def by_labels(first: str, second: str):
        pins = [SymbolPin("1", first), SymbolPin("2", second)]
        return part_kind("X", "Custom:Thing", plain, pins)

    assert by_labels("A", "B") is OTHER
    assert by_labels("C", "E") is OTHER
    assert by_labels("A", "K") is DIODE
    assert by_labels("A", "C") is DIODE


def test_sot23_against_the_recorded_c2132_footprint_gives_180():
    """KiCad's SOT-23 (pin 1 top-left) against EasyEDA's -BR drawing is exactly 180 degrees."""
    name, jlc, pins = load_jlc("C2132")
    verdict = resolve(KICAD_SOT23, "Package_TO_SOT_SMD:SOT-23", "ok", name, jlc, pins)
    assert verdict.status == "green"
    assert verdict.fit == "fits"
    assert verdict.rotation == 180
    assert verdict.method == "geometry"
    assert verdict.confidence == "high"
    assert verdict.name_rotation == 180
    assert verdict.pad_count_kicad == 3 and verdict.pad_count_jlc == 3
    assert verdict.overlap_min > 0.5


def test_soic8_bl_gives_270_and_agrees_with_the_name():
    """A -BL SOIC is KiCad's pattern turned 90 degrees CCW on screen: the CPL takes 270, the name's table says 90."""
    jlc = rotated_on_screen_ccw(KICAD_SOIC8, 90)
    verdict = resolve(
        KICAD_SOIC8,
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "ok",
        "SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL",
        jlc,
        [],
    )
    assert verdict.rotation == 270
    assert verdict.name_rotation == 270
    assert verdict.confidence == "high"
    assert verdict.status == "green"
    assert verdict.fit == "fits"


def test_placed_rotation_never_enters_the_footprint_frame():
    """The same part drawn at any of the four screen rotations yields the same correction."""
    jlc = rotated_on_screen_ccw(KICAD_SOIC8, 90)
    for degrees in (0, 90, 180, 270):
        turned = rotated_on_screen_ccw(KICAD_SOIC8, degrees)
        verdict = resolve(
            turned,
            "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            "ok",
            "SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL",
            jlc,
            [],
        )
        assert verdict.rotation == (270 + degrees) % 360, degrees


def test_non_standard_kicad_footprint_keeps_geometry_and_notes_the_name():
    """When the KiCad footprint is already drawn like JLC's, geometry says 0 and the name says 270."""
    already_bl = rotated_on_screen_ccw(KICAD_SOIC8, 90)
    verdict = resolve(
        already_bl,
        "Custom:SOIC-8_BL",
        "ok",
        "SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL",
        already_bl,
        [],
    )
    assert verdict.rotation == 0
    assert verdict.name_rotation == 270
    assert verdict.confidence == "medium"
    assert any("non-standard" in note for note in verdict.notes)


def test_mirrored_pin_order_is_red_not_solved():
    """A Y-mirrored pin order is a wrong footprint variant, flagged rather than rotated."""
    mirrored = [p._replace(y=-p.y) for p in KICAD_SOIC8]
    verdict = resolve(
        KICAD_SOIC8,
        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        "ok",
        "SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL",
        mirrored,
        [],
    )
    assert verdict.status == "red"
    assert verdict.fit == "mirror"
    assert verdict.rotation is None


def test_wrong_pick_with_different_pad_count_is_red_count():
    """Six KiCad pads against a five-pad JLC part with a pad that lands nowhere: red, count."""
    kicad_sot23_6 = [
        Pad("1", -1.3, -0.95, 1.0, 0.6),
        Pad("2", -1.3, 0.0, 1.0, 0.6),
        Pad("3", -1.3, 0.95, 1.0, 0.6),
        Pad("4", 1.3, 0.95, 1.0, 0.6),
        Pad("5", 1.3, 0.0, 1.0, 0.6),
        Pad("6", 1.3, -0.95, 1.0, 0.6),
    ]
    jlc_sot23_5 = [
        Pad("1", -1.3, -0.95, 1.0, 0.6),
        Pad("2", -1.3, 0.0, 1.0, 0.6),
        Pad("3", -1.3, 0.95, 1.0, 0.6),
        Pad("4", 1.3, 0.95, 1.0, 0.6),
        Pad("5", 1.3, -0.95, 1.0, 0.6),
    ]
    verdict = resolve(
        kicad_sot23_6,
        "Package_TO_SOT_SMD:SOT-23-6",
        "ok",
        "SOT-23-5_L2.9-W1.6-P0.95-LS2.8-BL",
        jlc_sot23_5,
        [],
    )
    assert verdict.status == "red"
    assert verdict.fit == "count"
    assert verdict.rotation is None
    assert "6 vs 5 pads" in verdict.note_text


def test_two_versus_three_pads_is_red_count():
    """A two-pad KiCad footprint cannot take a three-pad part."""
    jlc = KICAD_SOT23
    kicad = [Pad("1", -1.0, 0, 1, 1), Pad("2", 1.0, 0, 1, 1)]
    verdict = resolve(
        kicad, "Diode_SMD:D_SOD-123", "ok", "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR", jlc, []
    )
    assert (verdict.status, verdict.fit) == ("red", "count")


def test_fetch_error_is_unknown_and_will_retry():
    """A failed fetch is not a verdict about the part; the part is asked for again later."""
    verdict = resolve(KICAD_SOT23, "Package_TO_SOT_SMD:SOT-23", "error", "", [], [])
    assert (verdict.status, verdict.rotation, verdict.method) == (
        "unknown",
        None,
        "none",
    )
    assert "will retry" in verdict.note_text


def test_non_finite_pad_values_are_unknown_not_a_crash():
    """A NaN coordinate from a corrupt record short-circuits before any geometry."""
    package, jlc_pads, pins = load_jlc("C2132")
    bad = [KICAD_SOT23[0]._replace(x=float("nan"))] + KICAD_SOT23[1:]
    verdict = resolve(bad, "Package_TO_SOT_SMD:SOT-23", "ok", package, jlc_pads, pins)
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert "non-numeric" in verdict.note_text
    bad_jlc = [jlc_pads[0]._replace(width=float("inf"))] + jlc_pads[1:]
    verdict = resolve(
        KICAD_SOT23, "Package_TO_SOT_SMD:SOT-23", "ok", package, bad_jlc, pins
    )
    assert verdict.status == "unknown"


def test_fewer_than_two_shared_names_is_unknown():
    """One shared pad name cannot fix a rotation; say so instead of guessing."""
    package, jlc_pads, pins = load_jlc("C2132")
    renamed = [KICAD_SOT23[0]] + [
        p._replace(number=f"X{p.number}") for p in KICAD_SOT23[1:]
    ]
    verdict = resolve(
        renamed, "Package_TO_SOT_SMD:SOT-23", "ok", package, jlc_pads, pins
    )
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert "fewer than two matching pad names" in verdict.note_text


def test_coincident_multi_pin_pads_are_underdetermined():
    """Three KiCad pads at one point cannot fix a rotation: the solver reports none."""
    kicad = [Pad(str(i), 0.0, 0.0, 0.5, 0.5) for i in range(1, 4)]
    jlc = [Pad(str(i), float(i), 0.0, 0.5, 0.5) for i in range(1, 4)]
    verdict = resolve(kicad, "Custom:Stacked", "ok", "SOT-23-3", jlc, [])
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert "degenerate" in verdict.note_text


def test_larger_kicad_pads_are_reported_exactly():
    """Hand-solder pads: the part fits, and the grade names the reason."""
    package, jlc_pads, pins = load_jlc("C2132")
    wide = [p._replace(width=p.width * 1.6, height=p.height * 1.4) for p in KICAD_SOT23]
    verdict = resolve(
        wide, "Package_TO_SOT_SMD:SOT-23_Handsoldering", "ok", package, jlc_pads, pins
    )
    assert (verdict.status, verdict.fit, verdict.rotation) == (
        "green",
        "fits_larger_pads",
        180,
    )


def test_skewed_kicad_pads_are_a_pitch_finding():
    """A footprint whose pad pitch is wrong for the part misses a pad: red, pitch."""
    package, jlc_pads, pins = load_jlc("C2132")
    skewed = [
        KICAD_SOT23[0]._replace(y=-1.6),
        KICAD_SOT23[1]._replace(y=1.6),
        KICAD_SOT23[2],
    ]
    verdict = resolve(
        skewed, "Package_TO_SOT_SMD:SOT-23", "ok", package, jlc_pads, pins
    )
    assert (verdict.status, verdict.fit, verdict.rotation) == ("red", "pitch", None)
    assert "does not fit: pitch" in verdict.note_text


def test_numbering_note_reports_the_package_angle_with_the_by_name_sign():
    """A cyclic renumbering of a 90-degree-rotated SOT-23 aligns by shape at the by-name angle."""
    package, jlc_pads, pins = load_jlc("C2132")
    turned = rotated_on_screen_ccw(jlc_pads, 90)
    by_name = resolve(
        KICAD_SOT23, "Package_TO_SOT_SMD:SOT-23", "ok", package, turned, pins
    )
    assert by_name.rotation in (90, 270)
    cyclic = {"1": "2", "2": "3", "3": "1"}
    renumbered = [p._replace(number=cyclic[p.number]) for p in turned]
    verdict = resolve(
        KICAD_SOT23, "Package_TO_SOT_SMD:SOT-23", "ok", package, renumbered, pins
    )
    assert (verdict.status, verdict.fit, verdict.rotation) == ("red", "numbering", None)
    assert f"aligns at {by_name.rotation}°" in verdict.note_text


def test_no_easyeda_record_is_unknown_with_the_checkerboard_note():
    """Parts EasyEDA does not know get no rotation and the checkerboard wording."""
    verdict = resolve(KICAD_SOT23, "Package_TO_SOT_SMD:SOT-23", "none", "", [], [])
    assert verdict.status == "unknown"
    assert verdict.rotation is None
    assert "checkerboard" in verdict.note_text


def test_a_switch_numbered_1_1_2_2_fits_whatever_order_its_legs_are_listed_in():
    """Legs sharing a number pair by position, so a listing order is never a reversed pin order."""
    legs = [
        Pad("1", -3.0, -2.0, 1.0, 1.0),
        Pad("1", 3.0, -2.0, 1.0, 1.0),
        Pad("2", -3.0, 2.0, 1.0, 1.0),
        Pad("2", 3.0, 2.0, 1.0, 1.0),
    ]
    for degrees in (0, 90):
        for order in ([0, 1, 2, 3], [1, 0, 3, 2], [1, 0, 2, 3]):
            jlc = rotated_on_screen_ccw([legs[i] for i in order], degrees)
            verdict = resolve(
                legs, "Button_Switch_SMD:SW_SPST", "ok", "SW-SMD_4P", jlc, []
            )
            assert (verdict.status, verdict.fit) == ("green", "fits"), order
            assert verdict.rotation == (360 - degrees) % 360


def test_a_numbering_difference_counts_every_pad_not_every_number():
    """Four pads under three numbers, all landing when names are ignored, are a numbering difference."""
    kicad = [
        Pad("1", -3.0, -1.0, 1.0, 1.0),
        Pad("2", -3.0, 1.0, 1.0, 1.0),
        Pad("3", 3.0, -1.0, 1.0, 1.0),
        Pad("3", 3.0, 1.0, 1.0, 1.0),
    ]
    renumbered = [
        pad._replace(number=number) for pad, number in zip(kicad, ("2", "3", "1", "3"))
    ]
    verdict = resolve(kicad, "Custom:Four", "ok", "X", renumbered, [])
    assert (verdict.status, verdict.fit, verdict.rotation) == ("red", "numbering", None)
    assert "the package itself aligns at 0°" in verdict.note_text
