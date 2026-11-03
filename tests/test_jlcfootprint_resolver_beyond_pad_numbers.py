"""Tests for the resolver beyond pad-number pairing: distinct pad numbers, pairing by pin function, the drawings' + marks vote, and the body-overhang caveat."""

import pytest

from jlcfootprint.drawing import DrawingMarks, drawing_marks
from jlcfootprint.easyeda_parse import SymbolPin
from jlcfootprint.geometry import Pad, easyeda_pads_to_mm, rotate
from jlcfootprint.polarity import (
    DIODE,
    OTHER,
    function_terminals,
    is_no_function,
    part_kind,
)
from jlcfootprint.resolver import (
    SHARED_CENTRE_NOTE,
    SINGLE_NUMBER_NOTE,
    YELLOW_NOTE,
    body_threshold_mm,
    resolve,
)
from tests.jlcfootprint_support import pro_record
from tools.jlcfootprint.kicad_library import library_footprint, with_functions

KICAD_SMF = [
    Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "K"),
    Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "A"),
]
JLC_SMF = [Pad("1", -1.45, 0.0, 1.3, 1.4), Pad("2", 1.45, 0.0, 1.3, 1.4)]
D1_PINS = [SymbolPin("1", "K"), SymbolPin("2", "A")]
NUMBERED = [SymbolPin("1", "1"), SymbolPin("2", "2")]


def resolve_library(library, footprint, lcsc, functions=None, marks=True):
    """Resolve a KiCad library footprint against a Pro-recorded part, drawings included."""
    record = pro_record(lcsc)
    pads, courtyard = library_footprint(library, footprint)
    if functions:
        pads = with_functions(pads, functions)
    return resolve(
        pads,
        f"{library}:{footprint}",
        record.status,
        record.package_name,
        easyeda_pads_to_mm(record.pads),
        record.symbol_pins,
        marks=drawing_marks(record.symbol_shapes, record.footprint_shapes)
        if marks
        else None,
        kicad_courtyard=courtyard,
    )


# --- distinct pad numbers and pairing by function --------------------------


def test_dpak_with_a_stub_counts_three_pads_and_fits():
    """TO-252-3_TabPin2's tab and stub are one pad 2: C20717 (tab = pin 2 on both sides) is green at 0 with no count note."""
    verdict = resolve_library("Package_TO_SOT_SMD", "TO-252-3_TabPin2", "C20717")
    assert (verdict.status, verdict.rotation, verdict.method) == (
        "green",
        0,
        "geometry",
    )
    assert (verdict.pad_count_kicad, verdict.pad_count_jlc) == (3, 3)
    assert "pad counts differ" not in verdict.note_text
    assert verdict.confidence == "high"


def test_dpak_whose_jlc_drawing_numbers_the_tab_3_pairs_by_function():
    """C42441713's drawing calls the tab 3 and the S lead 2; the schematic's G/D/S pair the pins and the part fits at 270."""
    verdict = resolve_library(
        "Package_TO_SOT_SMD",
        "TO-252-3_TabPin2",
        "C42441713",
        {"1": "G", "2": "D", "3": "S"},
    )
    assert (verdict.status, verdict.rotation, verdict.confidence) == (
        "green",
        270,
        "medium",
    )
    assert "paired by pin function" in verdict.note_text
    assert "JLC pin 3 (D) is pad 2 on the footprint" in verdict.note_text
    assert "JLC pin 2 (S) is pad 3 on the footprint" in verdict.note_text
    assert "by elimination" not in verdict.note_text
    assert verdict.name_rotation == 270


def test_one_unmatched_function_pairs_by_elimination():
    """IN on the gate pad matches no symbol name; D and S pair and the gate follows by elimination."""
    verdict = resolve_library(
        "Package_TO_SOT_SMD",
        "TO-252-3_TabPin2",
        "C42441713",
        {"1": "IN", "2": "D", "3": "S"},
    )
    assert (verdict.status, verdict.rotation) == ("green", 270)
    assert "JLC pin 1 and pad 1 paired by elimination" in verdict.note_text


def test_without_pin_functions_the_numbering_difference_stays_red():
    """No functions, no pairing: the numbering finding stands, with the angle the package would take."""
    for footprint in ("TO-252-3_TabPin2", "TO-252-2"):
        verdict = resolve_library("Package_TO_SOT_SMD", footprint, "C42441713")
        assert (verdict.status, verdict.fit, verdict.rotation) == (
            "red",
            "numbering",
            None,
        )
        assert "the package itself aligns at 270°" in verdict.note_text


def test_a_function_pairing_is_never_described_as_shared_pad_names():
    """A two-pair function pairing carries neither the by-name note nor its counts wording."""
    kicad = [
        Pad("3", -1.0, 0.0, 1.0, 1.0, 0.0, "IN"),
        Pad("4", 1.0, 0.0, 1.0, 1.0, 0.0, "OUT"),
        Pad("5", 0.0, 2.0, 1.0, 1.0),
    ]
    jlc = [Pad("1", -1.0, 0.0, 1.0, 1.0), Pad("2", 1.0, 0.0, 1.0, 1.0)]
    verdict = resolve(
        kicad,
        "Custom:Two_Signals_And_A_Mount",
        "ok",
        "TEST-2PAD_L2.0-W1.0",
        jlc,
        [SymbolPin("1", "IN"), SymbolPin("2", "OUT")],
    )
    assert (verdict.status, verdict.rotation, verdict.confidence) == (
        "green",
        0,
        "medium",
    )
    assert "paired by pin function" in verdict.note_text
    assert "shared pad names" not in verdict.note_text
    assert "pad counts differ (3 vs 2) but the 2 paired pads align" in verdict.notes


# --- the drawings' + marks -----------------------------------------------


def test_tantalums_without_token_or_label_are_read_from_their_marks():
    """CASE-A and CASE-C parts carry + marks in both drawings; pad 1 is the left pad on one and the right pad on the other."""
    left = resolve_library("Capacitor_Tantalum_SMD", "CP_EIA-3216-18_Kemet-A", "C7175")
    assert (left.status, left.rotation, left.polarity_source, left.confidence) == (
        "green",
        0,
        "drawing",
        "medium",
    )
    assert "polarity from the + marks of the symbol and the footprint" in left.notes
    right = resolve_library("Capacitor_Tantalum_SMD", "CP_EIA-6032-20_AVX-F", "C140418")
    assert (right.rotation, right.polarity_source) == (180, "drawing")
    blind = resolve_library(
        "Capacitor_Tantalum_SMD", "CP_EIA-3216-18_Kemet-A", "C7175", marks=False
    )
    assert (blind.status, blind.rotation) == ("unknown", None)
    assert "polarity unknown" in blind.note_text


def test_reversed_numbering_tantalum_token_and_marks_agree():
    """C7192's -R-RD token, read as every capacitor's (band = negative), agrees with its marks: 180 from the token."""
    verdict = resolve_library("Capacitor_Tantalum_SMD", "CP_EIA-3528-15_AVX-H", "C7192")
    assert (verdict.rotation, verdict.polarity_source) == (180, "token")
    assert (
        verdict.confidence == "medium"
    )  # KiCad pad 1 = + is assumed, as on every CP_ footprint without functions
    assert "names the other pad" not in verdict.note_text
    wired = resolve_library(
        "Capacitor_Tantalum_SMD", "CP_EIA-3528-15_AVX-H", "C7192", {"1": "+", "2": "-"}
    )
    assert (wired.rotation, wired.confidence) == (180, "high")


def test_an_unchecked_token_on_a_molded_chip_tantalum_is_only_medium():
    """The molded-chip tantalum family, whose token is weak, resolves at medium with nothing to check it."""
    kicad = [
        Pad("1", -1.6, 0.0, 1.2, 1.4, 0.0, "+"),
        Pad("2", 1.6, 0.0, 1.2, 1.4, 0.0, "-"),
    ]
    jlc = [Pad("1", -1.6, 0.0, 1.2, 1.4), Pad("2", 1.6, 0.0, 1.2, 1.4)]
    chip = resolve(
        kicad, "Custom:CP", "ok", "CAP-SMD_L3.2-W1.6-FD", jlc, NUMBERED, marks=None
    )
    assert (chip.rotation, chip.polarity_source) == (0, "token")
    assert chip.confidence == "medium"
    assert "names the other pad" not in chip.note_text
    can = resolve(kicad, "Custom:CP", "ok", "CAP-SMD_BD3.2-L3.2-W3.2-FD", jlc, NUMBERED)
    assert (can.rotation, can.polarity_source, can.confidence) == (0, "token", "high")
    checked = resolve(
        kicad,
        "Custom:CP",
        "ok",
        "CAP-SMD_L3.2-W1.6-FD",
        jlc,
        NUMBERED,
        marks=DrawingMarks(positive_pad="1"),
    )
    assert (checked.rotation, checked.confidence) == (0, "high")


def test_a_single_mark_against_the_token_is_a_tie_and_therefore_inconsistent():
    """A diode whose footprint + sits at the cathode end contradicts its RD token one to one: no guess."""
    marks = DrawingMarks(positive_pad="1")
    verdict = resolve(
        JLC_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        JLC_SMF,
        NUMBERED,
        marks=marks,
    )
    assert (verdict.status, verdict.rotation) == ("red", None)
    assert verdict.note_text.endswith(
        "EasyEDA data inconsistent: name token and footprint + mark disagree"
    )


def test_token_and_label_outvote_a_footprint_mark():
    """Two sources against one: the token's pad stands, the outvoted mark is noted and confidence drops."""
    marks = DrawingMarks(positive_pad="1")
    verdict = resolve(
        KICAD_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        JLC_SMF,
        D1_PINS,
        marks=marks,
    )
    assert (verdict.status, verdict.rotation, verdict.polarity_source) == (
        "green",
        0,
        "token",
    )
    assert verdict.confidence == "medium"
    assert (
        "footprint + mark names the other pad; name token, symbol pin-1 label decide"
        in verdict.notes
    )
    assert verdict.polarity_light == "green"


def test_both_marks_outvote_a_live_label_and_the_light_follows_the_vote():
    """Two marks against a label: the marks decide, and pin 1's meaning comes from the vote, not the outvoted label."""
    marks = DrawingMarks(positive_pin="1", positive_pad="1")
    verdict = resolve(
        KICAD_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "DIO-SMD_L2.8-W1.8",
        JLC_SMF,
        D1_PINS,
        marks=marks,
    )
    assert (verdict.rotation, verdict.polarity_source) == (180, "drawing")
    # JLC's pad 1 is the anode by the vote and KiCad's pad 1 is the cathode.
    assert (verdict.polarity_light, verdict.status) == ("yellow", "yellow")
    assert YELLOW_NOTE in verdict.notes
    assert (
        "symbol pin-1 label names the other pad; symbol + mark, footprint + mark decide"
        in verdict.notes
    )


def test_the_light_is_known_whenever_the_vote_settles_pad_1():
    """Without any label the token or the marks still say what JLC's pad 1 is: green when it matches KiCad's pad 1, yellow when not."""
    token_only = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, NUMBERED
    )
    assert (token_only.polarity_light, token_only.status) == ("green", "green")
    reversed_token = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-FD", JLC_SMF, NUMBERED
    )
    assert (reversed_token.rotation, reversed_token.polarity_light) == (180, "yellow")
    marks_only = resolve(
        KICAD_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "DIO-SMD_L2.8-W1.8",
        JLC_SMF,
        NUMBERED,
        marks=DrawingMarks(positive_pad="2"),
    )
    assert (marks_only.rotation, marks_only.polarity_light) == (0, "green")


def test_token_label_and_symbol_mark_outvote_a_footprint_mark():
    """Marks that name different pads are two votes, not a veto: three sources against the footprint's mark decide."""
    marks = DrawingMarks(positive_pin="2", positive_pad="1")
    verdict = resolve(
        KICAD_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        JLC_SMF,
        D1_PINS,
        marks=marks,
    )
    assert (verdict.status, verdict.rotation, verdict.confidence) == (
        "green",
        0,
        "medium",
    )
    assert (
        "footprint + mark names the other pad; name token, symbol pin-1 label, symbol + mark decide"
        in verdict.notes
    )


def test_marks_that_contradict_each_other_are_inconsistent():
    """The symbol's + beside pin 1 and the footprint's + beside pad 2 cannot both be right."""
    marks = DrawingMarks(positive_pin="1", positive_pad="2")
    verdict = resolve(
        JLC_SMF,
        "Capacitor_SMD:CP_Elec_4x5.4",
        "ok",
        "CASE-A_3216",
        JLC_SMF,
        NUMBERED,
        marks=marks,
    )
    assert (verdict.status, verdict.rotation) == ("red", None)
    assert verdict.note_text.endswith(
        "EasyEDA data inconsistent: symbol + mark and footprint + mark disagree"
    )


def test_a_lone_footprint_mark_decides_when_nothing_else_speaks():
    """A part with no symbol pins: the footprint's own + mark is enough, at medium confidence."""
    marks = DrawingMarks(positive_pad="2")
    verdict = resolve(
        KICAD_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "DIO-SMD_L2.8-W1.8",
        JLC_SMF,
        NUMBERED,
        marks=marks,
    )
    assert (verdict.rotation, verdict.polarity_source, verdict.confidence) == (
        0,
        "drawing",
        "medium",
    )
    assert "polarity from the footprint's + mark" in verdict.notes


def test_reversed_numbering_with_pin_functions_pairs_by_function():
    """A JLC SOT-23 numbered the other way round is a mirror by number; the schematic's B/E/C pair the pins and the part fits as drawn."""
    kicad = [
        Pad("1", -0.95, 1.0, 0.6, 1.0, 0.0, "B"),
        Pad("2", 0.95, 1.0, 0.6, 1.0, 0.0, "E"),
        Pad("3", 0.0, -1.0, 0.6, 1.0, 0.0, "C"),
    ]
    jlc = [
        Pad("2", -0.95, 1.0, 0.6, 1.0),
        Pad("1", 0.95, 1.0, 0.6, 1.0),
        Pad("3", 0.0, -1.0, 0.6, 1.0),
    ]
    pins = [SymbolPin("1", "E"), SymbolPin("2", "B"), SymbolPin("3", "C")]
    paired = resolve(
        kicad, "Package_TO_SOT_SMD:SOT-23", "ok", "SOT-23-3_L2.9-W1.6", jlc, pins
    )
    assert (paired.status, paired.rotation, paired.confidence) == ("green", 0, "medium")
    assert "JLC pin 2 (B) is pad 1 on the footprint" in paired.note_text
    blind = resolve(
        [pad._replace(pin_function="") for pad in kicad],
        "Package_TO_SOT_SMD:SOT-23",
        "ok",
        "SOT-23-3_L2.9-W1.6",
        jlc,
        pins,
    )
    assert (blind.status, blind.fit, blind.rotation) == ("red", "mirror", None)


def test_pads_that_share_no_name_pair_by_function():
    """A JLC drawing naming its pads E, B and C shares no name with KiCad's 1, 2, 3; the schematic's functions pair them and the part fits as drawn."""
    kicad = [
        Pad("1", -0.95, 1.0, 0.6, 1.0, 0.0, "B"),
        Pad("2", 0.95, 1.0, 0.6, 1.0, 0.0, "E"),
        Pad("3", 0.0, -1.0, 0.6, 1.0, 0.0, "C"),
    ]
    jlc = [
        Pad("B", -0.95, 1.0, 0.6, 1.0),
        Pad("E", 0.95, 1.0, 0.6, 1.0),
        Pad("C", 0.0, -1.0, 0.6, 1.0),
    ]
    pins = [SymbolPin("B", "B"), SymbolPin("E", "E"), SymbolPin("C", "C")]
    paired = resolve(
        kicad, "Package_TO_SOT_SMD:SOT-23", "ok", "SOT-23-3_L2.9-W1.6", jlc, pins
    )
    assert (paired.status, paired.rotation, paired.confidence) == ("green", 0, "medium")
    assert "JLC pin B (B) is pad 1 on the footprint" in paired.note_text
    blind = resolve(
        [pad._replace(pin_function="") for pad in kicad],
        "Package_TO_SOT_SMD:SOT-23",
        "ok",
        "SOT-23-3_L2.9-W1.6",
        jlc,
        pins,
    )
    assert (blind.status, blind.fit) == ("unknown", "no_data")
    assert "fewer than two matching pad names" in blind.note_text


# --- pin functions that say "not connected" -------------------------------


def test_no_connection_functions_carry_no_terminal_and_disqualify_nothing():
    """NC-like functions are skipped, so K and A beside an NC pad still read as a diode."""
    assert is_no_function("NC") and is_no_function("n/c") and is_no_function("~")
    assert is_no_function("DNC") and is_no_function("NP") and is_no_function("")
    assert not is_no_function("K") and not is_no_function("VCC")
    pads = [
        Pad("1", -1.0, 0.0, 1.0, 1.0, 0.0, "K"),
        Pad("2", 1.0, 0.0, 1.0, 1.0, 0.0, "A"),
        Pad("3", 0.0, 1.5, 1.0, 0.5, 0.0, "NC"),
    ]
    assert function_terminals(pads) == {"cathode", "anode"}
    assert part_kind("MPN-3PAD", "Custom:Three", pads, []) is DIODE
    switch = [Pad("1", -1, 0, 1, 1, 0, "A"), Pad("2", 1, 0, 1, 1, 0, "B")]
    assert function_terminals(switch) == set()
    c_and_a = [Pad("1", -1, 0, 1, 1, 0, "C"), Pad("2", 1, 0, 1, 1, 0, "A")]
    assert part_kind("MPN-2PAD", "Custom:Two", c_and_a, []) is OTHER


def test_a_function_without_letters_names_a_pin():
    """``1``, ``+5V`` or ``~{CS}`` is a pin's name, not "not connected": beside K it is no diode."""
    for text in ("1", "+5V", "3V3", "/CS", "~{CS}"):
        assert not is_no_function(text), text
    pads = [Pad("1", -1, 0, 1, 1, 0, "K"), Pad("2", 1, 0, 1, 1, 0, "1")]
    assert function_terminals(pads) == set()
    assert part_kind("CONN-SMD_2P", "Connector:Conn_01x02", pads, []) is OTHER


def test_the_same_pads_numbered_with_repeats_are_a_numbering_difference():
    """Four pads at the same places, numbered 1, 2, 3, 4 and 1, 1, 2, 2: only the numbering differs."""

    def row(numbers: str) -> list[Pad]:
        return [Pad(n, x, 0, 0.6, 1.0) for n, x in zip(numbers, (0, 1.27, 2.54, 3.81))]

    verdict = resolve(row("1234"), "Custom:Row4", "ok", "X", row("1122"), [])
    assert (verdict.status, verdict.fit, verdict.rotation) == ("red", "numbering", None)
    assert "the package itself aligns at 0°" in verdict.note_text


# --- the body caveat ------------------------------------------------------


def test_body_larger_than_the_courtyard_is_a_caveat_not_a_failure():
    """C88744's 6.6 mm body on CP_Elec_4x5.8 fits its pads but overhangs the courtyard by 0.9 mm each side."""
    small = resolve_library("Capacitor_SMD", "CP_Elec_4x5.8", "C88744")
    assert (small.status, small.rotation, small.fit) == ("green", 0, "fits")
    assert small.body_excess_mm == pytest.approx(0.9, abs=0.01)
    assert "fits, JLC body 0.9 mm larger than the KiCad courtyard" in small.notes
    big = resolve_library("Capacitor_SMD", "CP_Elec_6.3x5.9", "C88744")
    assert big.body_excess_mm is None and "larger than" not in big.note_text
    dpak = resolve_library(
        "Package_TO_SOT_SMD",
        "TO-252-3_TabPin2",
        "C42441713",
        {"1": "G", "2": "D", "3": "S"},
    )
    assert dpak.body_excess_mm is None  # the courtyard is turned with the pads


def test_the_caveat_needs_a_courtyard_and_a_body_and_a_fit():
    """No courtyard, no body box or no rotation gives no caveat."""
    record = pro_record("C88744")
    pads, courtyard = library_footprint("Capacitor_SMD", "CP_Elec_4x5.8")
    jlc = easyeda_pads_to_mm(record.pads)
    body = DrawingMarks(
        positive_pin="1", positive_pad="1", body_box=(-3.3, -3.3, 3.3, 3.3)
    )
    assert (
        resolve(
            pads,
            "Capacitor_SMD:CP_Elec_4x5.8",
            "ok",
            record.package_name,
            jlc,
            record.symbol_pins,
            marks=body,
        ).body_excess_mm
        is None
    )
    without_body = DrawingMarks(positive_pin="1", positive_pad="1")
    assert (
        resolve(
            pads,
            "Capacitor_SMD:CP_Elec_4x5.8",
            "ok",
            record.package_name,
            jlc,
            record.symbol_pins,
            marks=without_body,
            kicad_courtyard=courtyard,
        ).body_excess_mm
        is None
    )
    wide = [Pad("1", -6.0, 0.0, 3.5, 1.6), Pad("2", 6.0, 0.0, 3.5, 1.6)]
    red = resolve(
        pads,
        "Capacitor_SMD:CP_Elec_4x5.8",
        "ok",
        record.package_name,
        wide,
        record.symbol_pins,
        marks=body,
        kicad_courtyard=courtyard,
    )
    assert red.status == "red" and red.body_excess_mm is None


def test_a_courtyard_with_a_zero_length_side_raises_no_caveat():
    """A degenerate courtyard measures nothing, so the body is not reported as overhanging it."""
    record = pro_record("C88744")
    pads, courtyard = library_footprint("Capacitor_SMD", "CP_Elec_4x5.8")
    jlc = easyeda_pads_to_mm(record.pads)
    body = DrawingMarks(
        positive_pin="1", positive_pad="1", body_box=(-3.3, -3.3, 3.3, 3.3)
    )

    def excess(box):
        return resolve(
            pads,
            "Capacitor_SMD:CP_Elec_4x5.8",
            "ok",
            record.package_name,
            jlc,
            record.symbol_pins,
            marks=body,
            kicad_courtyard=box,
        ).body_excess_mm

    assert excess(courtyard) == pytest.approx(0.9, abs=0.01)
    flat = (courtyard[0], 0.0, courtyard[2], 0.0)
    assert excess(flat) is None
    assert excess((0.0, courtyard[1], 0.0, courtyard[3])) is None


def test_body_threshold_is_relative_with_clamps():
    """Ten percent of the courtyard's shorter side, never below 0.15 mm nor above 1.5 mm."""
    assert body_threshold_mm((-0.7, -0.35, 0.7, 0.35)) == 0.15
    assert body_threshold_mm((-3.35, -2.4, 3.35, 2.4)) == pytest.approx(0.48)
    assert body_threshold_mm((-1.05, -1.53, 16.29, 49.78)) == 1.5


def test_pads_that_all_share_one_number_are_unknown():
    """A standoff's four pads, all numbered 1, cannot say which way round it goes.

    Turned 90 degrees, every JLC pad still lands on some KiCad pad, so pairing by
    name would call it green at 0.
    """
    kicad = [Pad("1", x, y, 2.95, 2.45) for x in (-1.6, 1.6) for y in (-1.35, 1.35)]
    turned = [Pad("1", -y, x, 2.45, 2.95) for x in (-1.6, 1.6) for y in (-1.35, 1.35)]
    for jlc in (kicad, turned):
        verdict = resolve(kicad, "Mounting:Standoff", "ok", "X", jlc, [])
        assert (verdict.status, verdict.rotation) == ("unknown", None)
        assert verdict.notes == [SINGLE_NUMBER_NOTE]


def test_two_pads_of_one_number_are_unknown_not_aligned_by_axis():
    """Two legs numbered 1, or a resistor on two pads that are one terminal, have no axis to give."""
    legs = [Pad("1", -2.0, 0.0, 1.0, 1.0), Pad("1", 2.0, 0.0, 1.0, 1.0)]
    resistor = [Pad("1", -2.0, 0.0, 1.0, 1.0), Pad("2", 2.0, 0.0, 1.0, 1.0)]
    for kicad, jlc in ((legs, legs), (legs, resistor), (resistor, legs)):
        verdict = resolve(kicad, "R_0805", "ok", "R0805", jlc, [])
        assert (verdict.status, verdict.rotation) == ("unknown", None)
        assert verdict.notes == [SINGLE_NUMBER_NOTE]


# A four-pad drawing whose two terminals are each split into two like pads, numbered
# 1, 1, 2, 2 as a PLCC-4 LED's are.
PLCC4 = [
    Pad("2", 1.7, 0.7, 1.2, 0.9),
    Pad("2", 1.7, -0.7, 1.2, 0.9),
    Pad("1", -1.7, -0.7, 1.2, 0.9),
    Pad("1", -1.7, 0.7, 1.2, 0.9),
]


def test_a_terminal_split_on_one_side_aligns_on_its_centre():
    """Pads 1, 1, 2, 2 against 1, 2, either way round, align on each number's centre.

    Solving on the one member pair the pairing chose put the placement half the
    members' spacing off: a false red with short KiCad pads, a green with the
    origin 0.7 mm out with tall ones.
    """
    for height in (2.6, 4.0):
        kicad = [Pad("1", -1.5, 0.0, 1.5, height), Pad("2", 1.5, 0.0, 1.5, height)]
        verdict = resolve(kicad, "Lib:Some_2pad", "ok", "X", PLCC4, [])
        assert (verdict.status, verdict.rotation) == ("green", 0), height
        assert verdict.origin == pytest.approx((0.0, 0.0), abs=1e-9)
    two = [Pad("1", -1.7, 0.0, 1.2, 2.3), Pad("2", 1.7, 0.0, 1.2, 2.3)]
    verdict = resolve(PLCC4, "Lib:Some_4pad", "ok", "X", two, [])
    assert verdict.rotation == 0
    assert verdict.origin == pytest.approx((0.0, 0.0), abs=1e-9)


def test_a_polarized_part_whose_drawing_splits_a_terminal_still_votes():
    """Two pad numbers are two terminals: a split cathode goes through the polarity vote.

    Counted by pads, the split diode went to the geometry path, which reads no pin
    function, label or token, and came back green at 0: the diode reversed.
    """
    kicad = [
        Pad("1", -2.0, 0.0, 1.5, 2.0, 0.0, "K"),
        Pad("2", 2.0, 0.0, 1.5, 2.0, 0.0, "A"),
    ]
    pins = [SymbolPin("1", "A"), SymbolPin("2", "K")]
    package = "SMA_L4.3-W2.6-LS5.2-FD"
    two = [Pad("1", -2.0, 0.0, 1.5, 2.0), Pad("2", 2.0, 0.0, 1.5, 2.0)]
    split = [
        Pad("1", -2.0, 0.0, 1.5, 2.0),
        Pad("2", 2.0, 0.5, 1.5, 1.0),
        Pad("2", 2.0, -0.5, 1.5, 1.0),
    ]
    whole = resolve(kicad, "Diode_SMD:D_SMA", "ok", package, two, pins)
    halves = resolve(kicad, "Diode_SMD:D_SMA", "ok", package, split, pins)
    assert (whole.method, whole.rotation) == ("polarity", 180)
    assert (halves.method, halves.rotation, halves.status) == (
        "polarity",
        180,
        whole.status,
    )
    assert halves.origin == pytest.approx(whole.origin, abs=1e-9)


def test_a_split_polarized_part_missing_a_central_pad_is_yellow():
    """JLC's central pad numbered 1, which the KiCad footprint lacks, is the caveat it is elsewhere."""
    kicad = [
        Pad("1", -2.0, 0.0, 2.0, 2.0, 0.0, "K"),
        Pad("2", 2.0, 0.0, 2.0, 2.0, 0.0, "A"),
    ]
    jlc = [
        Pad("1", -2.0, 0.0, 2.0, 2.0),
        Pad("2", 2.0, 0.0, 2.0, 2.0),
        Pad("1", 0.0, 0.0, 1.0, 1.0),
    ]
    pins = [SymbolPin("1", "K"), SymbolPin("2", "A")]
    verdict = resolve(kicad, "Diode_SMD:D_SMA", "ok", "SMA_L4-W2-LS6-RD", jlc, pins)
    assert (verdict.method, verdict.rotation) == ("polarity", 0)
    assert verdict.missing_central_pad
    assert verdict.status == "yellow"
    assert "JLC part has a central pad (1) your footprint lacks" in verdict.notes


def _turned(pads, degrees):
    """Return the pads turned by ``degrees`` about the footprint origin."""
    turned = []
    for pad in pads:
        x, y = rotate(pad.x, pad.y, degrees)
        turned.append(pad._replace(x=x, y=y, rotation=(pad.rotation + degrees) % 360))
    return turned


def test_terminals_that_share_a_centre_align_by_number():
    """A coin cell's + tabs either side of its - pad: no axis by meaning, the layout decides.

    Both terminals' centres sit on the middle pad, so the polarity path has nothing
    to align on; the pads by number fix the turn, and swapped numbers would not fit.
    """
    holder = [
        Pad("1", -5.0, 0.0, 2.0, 3.0),
        Pad("1", 5.0, 0.0, 2.0, 3.0),
        Pad("2", 0.0, 0.0, 4.0, 4.0),
    ]
    for degrees in (0, 90):
        verdict = resolve(
            holder,
            "Battery:BatteryHolder_Coin",
            "ok",
            "X",
            _turned(holder, degrees),
            [],
        )
        assert SHARED_CENTRE_NOTE in verdict.notes
        assert (verdict.method, verdict.rotation) == ("geometry", degrees)


# The coin holder above with KiCad's pin functions: the tabs are -, the centre pad +.
SIGNED_HOLDER = [
    Pad("1", -5.0, 0.0, 2.0, 3.0, pin_function="-"),
    Pad("1", 5.0, 0.0, 2.0, 3.0, pin_function="-"),
    Pad("2", 0.0, 0.0, 4.0, 4.0, pin_function="+"),
]


def _holder(kicad, jlc_pins, positive_pin):
    """Resolve the shared-centre holder against JLC's unlabelled pads, pins and + mark."""
    jlc = [pad._replace(pin_function="") for pad in SIGNED_HOLDER]
    marks = DrawingMarks(positive_pin=positive_pin)
    return resolve(kicad, "Battery:BatteryHolder_Coin", "ok", "X", jlc, jlc_pins, marks)


def test_terminals_that_share_a_centre_keep_the_meaning_their_numbers_give():
    """Aligned by number, the + must be one number on both sides: no turn can swap them."""
    agrees = _holder(SIGNED_HOLDER, [SymbolPin("1", "-"), SymbolPin("2", "+")], "2")
    assert (agrees.status, agrees.method, agrees.rotation) == ("green", "geometry", 0)
    reversed_ = _holder(SIGNED_HOLDER, [SymbolPin("1", "+"), SymbolPin("2", "-")], "1")
    assert (reversed_.status, reversed_.fit, reversed_.rotation) == (
        "red",
        "numbering",
        None,
    )
    assert reversed_.notes[-1] == (
        "KiCad pad 2 is the positive terminal but JLC's is pad 1, "
        "and terminals that share a centre cannot swap"
    )


def test_terminals_that_share_a_centre_are_red_on_inconsistent_evidence():
    """A pin-1 label and a + mark naming different pads tie, as on any polarized part."""
    verdict = _holder(SIGNED_HOLDER, [SymbolPin("1", "+"), SymbolPin("2", "-")], "2")
    assert (verdict.status, verdict.rotation) == ("red", None)
    assert verdict.notes[-1] == (
        "EasyEDA data inconsistent: symbol pin-1 label and symbol + mark disagree"
    )


def test_terminals_that_share_a_centre_on_an_assumed_pad_1_are_yellow():
    """Without KiCad pin functions pad 1 is only assumed to be +, so JLC's + on pad 2 is a caveat."""
    unsigned = [pad._replace(pin_function="") for pad in SIGNED_HOLDER]
    verdict = _holder(unsigned, [], "2")
    assert (verdict.status, verdict.rotation, verdict.confidence) == (
        "yellow",
        0,
        "medium",
    )
    assert verdict.notes[-1] == (
        "assumed KiCad pad 1 = +, but JLC puts the positive terminal on pad 2: "
        "check the polarity"
    )


def test_terminals_that_share_a_centre_stand_on_numbers_unless_kicad_contradicts_itself():
    """No KiCad pad 1 to assume leaves the numbers to decide; two + pads are unknown."""
    lettered = [pad._replace(number="AB"[int(pad.number) - 1]) for pad in SIGNED_HOLDER]
    unsigned = [pad._replace(pin_function="") for pad in lettered]
    marks = DrawingMarks(positive_pin="A")
    stands = resolve(
        unsigned, "Battery:BatteryHolder_Coin", "ok", "X", unsigned, [], marks
    )
    assert (stands.status, stands.rotation) == ("green", 0)
    both_plus = [pad._replace(pin_function="+") for pad in SIGNED_HOLDER]
    verdict = _holder(both_plus, [SymbolPin("1", "-"), SymbolPin("2", "+")], "2")
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert verdict.notes[-1] == "contradictory pin functions on the KiCad footprint"


def test_a_split_polarized_part_with_nothing_to_vote_is_unknown():
    """Split terminals still need a vote: with no token, label or mark nothing is guessed."""
    holder = [
        Pad("1", -6.0, -2.0, 2.0, 2.0),
        Pad("1", -6.0, 2.0, 2.0, 2.0),
        Pad("2", 6.0, 0.0, 3.0, 5.0),
    ]
    verdict = resolve(holder, "Battery:BatteryHolder_Clip", "ok", "X", holder, [])
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert "polarity unknown; check in JLC preview" in verdict.notes
