"""Resolver tests on real KiCad library footprints against recorded EasyEDA responses."""

import math

import pytest

from jlcfootprint.drawing import drawing_marks
from jlcfootprint.fit import pair_by_name
from jlcfootprint.geometry import Pad, easyeda_pads_to_mm, rotate
from jlcfootprint.naming import parse_package_name
from jlcfootprint.resolver import resolve
from tests.jlcfootprint_support import recorded
from tools.jlcfootprint.kicad_library import (
    footprints_available,
    library_pads,
    with_functions,
)

pytestmark = pytest.mark.skipif(
    not footprints_available(),
    reason="no KiCad footprint snapshots and no installed KiCad libraries",
)


def verdict_for(library, footprint, lcsc, functions=None):
    """Resolve one KiCad library footprint against one recorded part."""
    record = recorded(lcsc)
    pads = library_pads(library, footprint)
    if functions:
        pads = with_functions(pads, functions)
    return resolve(
        pads,
        f"{library}:{footprint}",
        record.status,
        record.package_name,
        easyeda_pads_to_mm(record.pads),
        record.symbol_pins,
        marks=drawing_marks(
            record.symbol_shapes, record.footprint_shapes, record.footprint_origin
        ),
    )


def test_pair_by_name_pairs_duplicates_by_closest_area():
    """KiCad's SOT-223 tab is a second pad 2; the pin pairs with JLC's pin 2, the tab is left over."""
    record = recorded("C6186")
    kicad, jlc, leftovers = pair_by_name(
        library_pads("Package_TO_SOT_SMD", "SOT-223-3_TabPin2"),
        easyeda_pads_to_mm(record.pads),
    )
    assert set(kicad) == {"1", "2", "3"}
    assert kicad["2"].height < 2.0
    assert [pad.number for pad in leftovers] == ["4"]


def test_dpak3_tab_pairs_with_tab_not_with_the_stub_pin():
    """TO-252-3: KiCad numbers both the stub pin and the tab as 2; area pairing meets JLC's tab."""
    pads = library_pads("Package_TO_SOT_SMD", "TO-252-3_TabPin2")
    jlc = [Pad("1", -5, -2.3, 2, 1), Pad("2", 1.3, 0, 6, 6), Pad("3", -5, 2.3, 2, 1)]
    kicad, _, leftovers = pair_by_name(pads, jlc)
    assert kicad["2"].width > 5
    assert leftovers == []


def test_sot223_pins_align_and_the_tab_is_matched_by_position():
    """KiCad pins 1-3 on the left with the tab numbered 2; EasyEDA pins on the right, tab 4."""
    verdict = verdict_for("Package_TO_SOT_SMD", "SOT-223-3_TabPin2", "C6186")
    assert (verdict.status, verdict.fit, verdict.rotation) == ("green", "fits", 180)
    assert verdict.matched_pads == 3
    assert verdict.name_rotation == 180


def test_to252_aligns_on_two_shared_names_with_medium_confidence():
    """DPAK: only pins 1 and 3 share names (KiCad tab is 2, EasyEDA tab is 4)."""
    verdict = verdict_for("Package_TO_SOT_SMD", "TO-252-2", "C58069")
    assert (verdict.status, verdict.fit, verdict.rotation) == ("green", "fits", 0)
    assert verdict.confidence == "medium"
    assert verdict.matched_pads == 2
    assert any("two shared pad names" in note for note in verdict.notes)


def test_usb_c_aligns_on_lettered_pad_names():
    """A1/B1-style names match as strings; merged EasyEDA pads land on KiCad's stacked pads."""
    verdict = verdict_for(
        "Connector_USB", "USB_C_Receptacle_HRO_TYPE-C-31-M-12", "C165948"
    )
    assert verdict.status == "green"
    assert verdict.fit in ("fits", "fits_larger_pads")
    assert verdict.rotation == 0
    assert verdict.matched_pads >= 8
    assert verdict.name_rotation is None


def test_hand_solder_0603_pads_fit_the_part():
    """Hand-solder pads sit further out but still cover JLC's pads; that is not a pitch error."""
    verdict = verdict_for(
        "Resistor_SMD", "R_0603_1608Metric_Pad0.98x0.95mm_HandSolder", "C25804"
    )
    assert (verdict.status, verdict.rotation, verdict.method) == ("green", 0, "axis")
    assert verdict.fit in ("fits", "fits_larger_pads")


def test_0402_pads_under_an_0603_part_are_tight():
    """JLC's pad centres fall outside the inner 80 percent of KiCad's pads: yellow, tight."""
    verdict = verdict_for("Resistor_SMD", "R_0402_1005Metric", "C25804")
    assert (verdict.status, verdict.fit, verdict.rotation) == (
        "yellow",
        "fits_tight",
        0,
    )
    assert "edge of your pads" in verdict.note_text


def test_tantalum_without_token_or_label_is_read_from_its_drawings():
    """CASE-B_3528 has no FD/RD token and its symbol pins are just 1 and 2: the + marks decide."""
    verdict = verdict_for("Capacitor_Tantalum_SMD", "CP_EIA-3528-21_Kemet-B", "C16133")
    assert (verdict.status, verdict.rotation, verdict.polarity_source) == (
        "green",
        0,
        "drawing",
    )
    assert verdict.confidence == "medium"
    assert "polarity from the + marks of the symbol and the footprint" in verdict.notes
    record = recorded("C16133")
    verdict = resolve(
        library_pads("Capacitor_Tantalum_SMD", "CP_EIA-3528-21_Kemet-B"),
        "Capacitor_Tantalum_SMD:CP_EIA-3528-21_Kemet-B",
        record.status,
        record.package_name,
        easyeda_pads_to_mm(record.pads),
        record.symbol_pins,
    )
    assert verdict.status == "unknown"
    assert verdict.rotation is None
    assert "polarity unknown" in verdict.note_text


def test_reddit_pair_from_the_library_footprint():
    """D_SMF with K on pad 1: C1981006 (RD, pin 1 = K) is 0 and green; C1856655 (FD-1, pin 1 = A) is 180 and yellow."""
    functions = {"1": "K", "2": "A"}
    d10 = verdict_for("Diode_SMD", "D_SMF", "C1981006", functions)
    d9 = verdict_for("Diode_SMD", "D_SMF", "C1856655", functions)
    assert (d10.rotation, d10.status, d10.polarity_light) == (0, "green", "green")
    assert (d9.rotation, d9.status, d9.polarity_light) == (180, "yellow", "yellow")


def test_wrong_pick_sot23_5_on_sot23_6_pads_is_red_count():
    """Six KiCad pads, five JLC pads, and pad 5 lands on nothing."""
    verdict = verdict_for("Package_TO_SOT_SMD", "SOT-23-6", "C3014306")
    assert (verdict.status, verdict.fit) == ("red", "count")
    assert "6 vs 5 pads" in verdict.note_text


def test_pad_type_is_unchanged_by_functions():
    """with_functions keeps geometry and only fills the function."""
    (pad,) = with_functions([Pad("1", 1.0, 2.0, 3.0, 4.0)], {"1": "K"})
    assert pad == Pad("1", 1.0, 2.0, 3.0, 4.0, 0.0, "K")


def test_sod323_with_smaller_kicad_pads_still_fits():
    """KiCad's SOD-323 pads are smaller than JLC's land pattern; the part still lands on them."""
    verdict = verdict_for("Diode_SMD", "D_SOD-323", "C7502694", {"1": "K", "2": "A"})
    assert (verdict.status, verdict.fit, verdict.rotation) == ("green", "fits", 0)
    assert any("smaller" in note for note in verdict.notes)


def test_missing_exposed_pad_is_a_warning_not_a_wrong_pick():
    """A JLC part with a central pad the KiCad footprint lacks still gets its rotation, in yellow."""
    kicad = library_pads("Package_SO", "MSOP-10_3x3mm_P0.5mm")
    jlc = [pad._replace(x=pad.x, y=pad.y) for pad in kicad] + [
        Pad("11", 0.0, 0.0, 1.6, 2.2)
    ]
    verdict = resolve(
        kicad,
        "Package_SO:MSOP-10_3x3mm_P0.5mm",
        "ok",
        "MSOP-10_L3.0-W3.0-P0.50-LS5.0-BL-EP",
        jlc,
        [],
    )
    assert (verdict.status, verdict.fit, verdict.rotation) == ("yellow", "fits", 0)
    assert any("central pad" in note for note in verdict.notes)


def test_marked_inductor_honours_the_direction_token():
    """C77110 (L0402-RD) is not polarized: the token puts pin 1 on the right, so KiCad's pad 1 lands on JLC's pad 2 at 180."""
    verdict = verdict_for("Inductor_SMD", "L_0402_1005Metric", "C77110")
    assert (verdict.rotation, verdict.method, verdict.status) == (
        180,
        "polarity",
        "green",
    )
    assert verdict.name_rotation == 180
    assert verdict.non_polar
    assert verdict.notes == [
        "orientation token on a non-polar part: KiCad pad 1 goes where the token puts pin 1, which need not be JLC's pad 1",
        "assumed KiCad pad 1 = the token's pin 1",
    ]
    jlc = easyeda_pads_to_mm(recorded("C77110").pads)
    assert [pad.number for pad in sorted(jlc, key=lambda pad: pad.x)] == ["1", "2"]
    kicad_pad_1 = library_pads("Inductor_SMD", "L_0402_1005Metric")[0]
    x, y = rotate(kicad_pad_1.x, kicad_pad_1.y, verdict.placement.rotation_deg)
    landed = min(
        jlc,
        key=lambda pad: math.hypot(
            pad.x - x - verdict.placement.offset_x,
            pad.y - y - verdict.placement.offset_y,
        ),
    )
    assert (kicad_pad_1.number, landed.number) == ("1", "2")


def test_sot23_with_clockwise_numbering_is_a_numbering_finding():
    """JLC's SOT-23 -CW variant stacks pins 1 and 3; the package fits at 180 but the numbers do not."""
    kicad = library_pads("Package_TO_SOT_SMD", "SOT-23")
    jlc = [
        Pad("2", -1.25, 0.0, 1.25, 0.7),
        Pad("3", 1.25, -0.95, 1.25, 0.7),
        Pad("1", 1.25, 0.95, 1.25, 0.7),
    ]
    verdict = resolve(
        kicad,
        "Package_TO_SOT_SMD:SOT-23",
        "ok",
        "SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR-CW",
        jlc,
        [],
    )
    assert (verdict.status, verdict.fit, verdict.rotation) == ("red", "numbering", None)
    assert "aligns at 180" in verdict.note_text


def test_sod882_is_a_diode_family_for_the_name_cross_check():
    """SOD-882 is a cathode-first diode family: RD with pin 1 = K reads 0."""
    assert parse_package_name("SOD-882_L1.0-W0.6-RD").rotation_correction == 0


def test_wider_lead_span_sod323_is_tight_not_wrong():
    """JLC's LS2.7 SOD-323 pads sit at the outer edge of KiCad's D_SOD-323 pads."""
    kicad = with_functions(library_pads("Diode_SMD", "D_SOD-323"), {"1": "K", "2": "A"})
    jlc = [Pad("2", 1.3, 0.0, 0.9, 0.7), Pad("1", -1.3, 0.0, 0.9, 0.7)]
    verdict = resolve(
        kicad, "Diode_SMD:D_SOD-323", "ok", "SOD-323_L1.6-W1.3-LS2.7-RD", jlc, []
    )
    assert (verdict.status, verdict.fit, verdict.rotation) == (
        "yellow",
        "fits_tight",
        0,
    )


def test_custom_shaped_kicad_pad_is_not_judged_for_fit():
    """KiCad's SOT-89-3 tab is a custom pad whose stored size is only an anchor; JLC's tab lands on it."""
    verdict = verdict_for("Package_TO_SOT_SMD", "SOT-89-3", "C6186")
    assert verdict.status in ("green", "yellow", "red")
    kicad = library_pads("Package_TO_SOT_SMD", "SOT-89-3")
    assert any(pad.shape == "custom" for pad in kicad)
    jlc = [
        Pad("3", 1.25, -1.5, 0.7, 1.9),
        Pad("1", 1.25, 1.5, 0.7, 1.9),
        Pad("2", -1.25, 0.0, 2.0, 3.5),
        Pad("2", 1.15, 0.0, 2.1, 0.7),
    ]
    verdict = resolve(
        kicad,
        "Package_TO_SOT_SMD:SOT-89-3",
        "ok",
        "SOT-89-3_L4.5-W2.5-P1.50-LS4.1-BR",
        jlc,
        [],
    )
    assert (verdict.status, verdict.rotation) == ("green", 180)
    assert "custom-shaped" in verdict.note_text
