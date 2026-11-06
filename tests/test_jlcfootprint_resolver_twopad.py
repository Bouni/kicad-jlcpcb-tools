"""Tests for the two-pad paths: alignment by terminal meaning, and by axis."""

from jlcfootprint.drawing import DrawingMarks
from jlcfootprint.easyeda_parse import SymbolPin
from jlcfootprint.geometry import Pad
from jlcfootprint.polarity import (
    kicad_reference_pad,
    label_reference_pad,
    token_reference_side,
)
from jlcfootprint.resolver import (
    NO_VOTE_NOTE,
    NON_POLAR_TOKEN_NOTE,
    SINGLE_PAD_NOTE,
    YELLOW_NOTE,
    resolve,
)

# KiCad Diode_SMD:D_SMF with the schematic's K on pad 1 and A on pad 2.
KICAD_SMF = [
    Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "K"),
    Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "A"),
]
# EasyEDA SMF drawn with the same geometry (JLC zero orientation).
JLC_SMF = [Pad("1", -1.45, 0.0, 1.3, 1.4), Pad("2", 1.45, 0.0, 1.3, 1.4)]
D1_PINS = [SymbolPin("1", "K"), SymbolPin("2", "A")]
D2_PINS = [SymbolPin("1", "A"), SymbolPin("2", "K")]


def test_token_reference_side():
    """FD puts anode/positive on the left; RD the reverse; BI is none; no token is None."""
    assert token_reference_side("SMF_L2.8-W1.8-LS3.7-FD-1", "cathode") == "right"
    assert token_reference_side("SMF_L2.8-W1.8-LS3.7-RD", "cathode") == "left"
    assert token_reference_side("CAP-SMD_BD6.3-L6.6-W6.6-FD", "positive") == "left"
    assert token_reference_side("CAP-SMD_BD6.3-L6.6-W6.6-RD", "positive") == "right"
    assert token_reference_side("SMB_L4.3-W3.6-LS5.4-BI", "cathode") == "none"
    assert token_reference_side("DIO-SMD_L2.8-W1.8", "cathode") is None


def test_kicad_reference_pad_by_function_or_convention():
    """The named terminal wins; the other named terminal implies it; pad 1 is the fallback."""
    assert kicad_reference_pad(KICAD_SMF, "cathode") == (KICAD_SMF[0], False, "")
    only_anode = [Pad("1", -1, 0, 1, 1, 0, "A"), Pad("2", 1, 0, 1, 1)]
    assert kicad_reference_pad(only_anode, "cathode") == (only_anode[1], False, "")
    pad, assumed, note = kicad_reference_pad(JLC_SMF, "cathode")
    assert (pad, assumed, note) == (JLC_SMF[0], True, "assumed KiCad pad 1 = K")
    pad, assumed, note = kicad_reference_pad(
        [Pad("A", 0, 0, 1, 1), Pad("B", 1, 0, 1, 1)], "positive"
    )
    assert (pad, assumed) == (None, True)
    assert "no pad 1" in note


def test_kicad_reference_pad_reads_positive_and_negative_as_anode_and_cathode():
    """An LED symbol labelled +/- names the anode and cathode; both vocabularies match."""
    led = [Pad("1", -1, 0, 1, 1, 0, "-"), Pad("2", 1, 0, 1, 1, 0, "+")]
    assert kicad_reference_pad(led, "cathode", diode=True) == (led[0], False, "")
    cap = [Pad("1", -1, 0, 1, 1, 0, "K"), Pad("2", 1, 0, 1, 1, 0, "A")]
    assert kicad_reference_pad(cap, "positive") == (cap[1], False, "")


def test_contradictory_pin_functions_give_no_reference_pad():
    """Two pads both claiming the cathode cannot be trusted; the caller reports unknown."""
    both_k = [Pad("1", -1, 0, 1, 1, 0, "K"), Pad("2", 1, 0, 1, 1, 0, "K")]
    pad, assumed, note = kicad_reference_pad(both_k, "cathode", diode=True)
    assert pad is None
    assert "contradictory" in note
    verdict = resolve(
        both_k, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert "contradictory" in verdict.note_text


def test_c_is_a_cathode_only_on_a_diode():
    """``C`` is a collector on a transistor symbol; it names the cathode only for diodes."""
    pads = [Pad("1", -1, 0, 1, 1, 0, "C"), Pad("2", 1, 0, 1, 1, 0, "E")]
    assert kicad_reference_pad(pads, "cathode") == (
        pads[0],
        True,
        "assumed KiCad pad 1 = K",
    )
    assert kicad_reference_pad(pads, "cathode", diode=True) == (pads[0], False, "")


def test_led_with_plus_minus_functions_is_a_diode_read_by_meaning():
    """C84256-style: LED0805-RD, KiCad pads labelled - (pad 1) and + (pad 2).

    The footprint name says diode, so ``-`` is the cathode on pad 1: the same terminal
    JLC's pin 1 = K names, so the rotation is 0.  Read as a capacitor, with the
    functions ignored, the part would come out at 180.
    """
    led = [
        Pad("1", -1.05, 0.0, 1.0, 1.2, 0.0, "-"),
        Pad("2", 1.05, 0.0, 1.0, 1.2, 0.0, "+"),
    ]
    jlc = [Pad("1", -1.05, 0.0, 1.0, 1.2), Pad("2", 1.05, 0.0, 1.0, 1.2)]
    verdict = resolve(
        led, "LED_SMD:LED_0805_2012Metric", "ok", "LED0805-RD", jlc, D1_PINS
    )
    assert (verdict.rotation, verdict.status, verdict.polarity_light) == (
        0,
        "green",
        "green",
    )
    assert verdict.method == "polarity"


def test_led_symbol_labelled_plus_minus_on_the_easyeda_side_is_still_a_diode():
    """EasyEDA symbols for LEDs often say +/-; the KiCad footprint name keeps it a diode."""
    led = [
        Pad("1", -1.05, 0.0, 1.0, 1.2, 0.0, "K"),
        Pad("2", 1.05, 0.0, 1.0, 1.2, 0.0, "A"),
    ]
    jlc = [Pad("1", -1.05, 0.0, 1.0, 1.2), Pad("2", 1.05, 0.0, 1.0, 1.2)]
    plus_minus = [SymbolPin("1", "-"), SymbolPin("2", "+")]
    verdict = resolve(
        led, "LED_SMD:LED_0805_2012Metric", "ok", "LED0805-RD", jlc, plus_minus
    )
    assert (verdict.rotation, verdict.polarity_light) == (0, "green")


def test_two_terminal_part_on_a_three_pad_kicad_footprint_aligns_by_meaning():
    """A KiCad diode footprint with an extra pad still pairs its 1 and 2 by terminal."""
    kicad = [
        Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "K"),
        Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "A"),
        Pad("3", 0.0, 1.5, 1.0, 0.5),
    ]
    verdict = resolve(
        kicad, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert (verdict.rotation, verdict.method, verdict.status) == (
        0,
        "polarity",
        "green",
    )
    no_pair = [
        Pad("A", -1.45, 0.0, 1.3, 1.4, 0.0, "K"),
        Pad("B", 1.45, 0.0, 1.3, 1.4, 0.0, "A"),
        Pad("C", 0.0, 1.5, 1.0, 0.5),
    ]
    verdict = resolve(
        no_pair, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert (verdict.status, verdict.rotation) == ("unknown", None)


def test_degenerate_two_pad_geometry_is_unknown():
    """Two pads at the same point cannot define an axis: unknown, not a rotation."""
    stacked = [
        Pad("1", 0.0, 0.0, 1.0, 1.0, 0.0, "K"),
        Pad("2", 0.0, 0.0, 1.0, 1.0, 0.0, "A"),
    ]
    verdict = resolve(
        stacked, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert "degenerate" in verdict.note_text
    same = resolve(
        stacked, "Resistor_SMD:R_0603_1608Metric", "ok", "R0603", JLC_SMF, []
    )
    assert (same.status, same.rotation) == ("unknown", None)


def test_label_reference_pad():
    """Pin 1 = K means the cathode is pad 1; pin 1 = A means it is the other pad."""
    assert label_reference_pad(JLC_SMF, "K", "cathode") is JLC_SMF[0]
    assert label_reference_pad(JLC_SMF, "A", "cathode") is JLC_SMF[1]
    assert label_reference_pad(JLC_SMF, "A", "positive") is JLC_SMF[0]
    assert label_reference_pad(JLC_SMF, "K", "positive") is JLC_SMF[1]
    assert label_reference_pad(JLC_SMF, None, "cathode") is None


def test_reddit_d1_rd_with_pin1_cathode_is_0_and_green():
    """C1978115: reverse direction, EasyEDA pin 1 = K, same meaning as KiCad's pad 1."""
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert verdict.rotation == 0
    assert (verdict.status, verdict.polarity_light) == ("green", "green")
    assert (verdict.method, verdict.confidence) == ("polarity", "high")
    assert verdict.name_rotation == 0
    assert verdict.fit == "fits"


def test_reddit_d2_fd_with_pin1_anode_is_180_and_yellow():
    """C1856655: forward direction, EasyEDA pin 1 = A; a pad-number match would give 0."""
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-FD-1", JLC_SMF, D2_PINS
    )
    assert verdict.rotation == 180
    assert (verdict.status, verdict.polarity_light) == ("yellow", "yellow")
    assert "do not renumber" in verdict.note_text
    assert verdict.name_rotation == 180


def test_kicad_footprint_with_anode_on_pad_1_flips_the_answer():
    """The KiCad side is read by meaning too: anode on pad 1 turns D1's 0 into 180."""
    wired_backwards = [
        Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "A"),
        Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "K"),
    ]
    verdict = resolve(
        wired_backwards,
        "Diode_SMD:D_SMF",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        JLC_SMF,
        D1_PINS,
    )
    assert verdict.rotation == 180
    assert verdict.polarity_light == "yellow"


def test_unnamed_pins_fall_back_to_pad_1_cathode_with_medium_confidence():
    """Without pin functions the KiCad convention pad 1 = K is assumed and said so."""
    verdict = resolve(
        JLC_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-FD-1", JLC_SMF, D2_PINS
    )
    assert verdict.rotation == 180
    assert verdict.confidence == "medium"
    assert any("assumed KiCad pad 1 = K" in note for note in verdict.notes)


def test_polarized_cap_fd_is_0_by_convention_and_rd_is_180():
    """Electrolytics use pad 1 = + on both sides, so FD is 0 and RD is 180."""
    kicad = [Pad("1", -2.7, 0, 3.5, 1.6), Pad("2", 2.7, 0, 3.5, 1.6)]
    jlc = [Pad("1", -2.7, 0, 3.5, 1.6), Pad("2", 2.7, 0, 3.5, 1.6)]
    cap_pins = [SymbolPin("1", "+"), SymbolPin("2", "-")]
    fd = resolve(
        kicad,
        "Capacitor_SMD:CP_Elec_6.3x7.7",
        "ok",
        "CAP-SMD_BD6.3-L6.6-W6.6-FD",
        jlc,
        cap_pins,
    )
    assert (fd.rotation, fd.status, fd.polarity_light, fd.name_rotation) == (
        0,
        "green",
        "green",
        0,
    )
    rd = resolve(
        kicad,
        "Capacitor_SMD:CP_Elec_6.3x7.7",
        "ok",
        "CAP-SMD_BD6.3-L6.6-W6.6-RD",
        jlc,
        [],
    )
    # RD: JLC's pad 1 is the negative end, KiCad's pad 1 the positive: the marker warning.
    assert (rd.rotation, rd.polarity_light, rd.name_rotation) == (180, "yellow", 180)


def test_inconsistent_token_and_label_is_red():
    """FD says cathode right, the symbol says pin 1 (left) is K: data conflict, no guess."""
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-FD", JLC_SMF, D1_PINS
    )
    assert verdict.status == "red"
    assert verdict.rotation is None
    assert "inconsistent" in verdict.note_text


def test_no_polarity_source_is_unknown_never_guessed():
    """No token and no labels: a polarized part is left for the JLC preview."""
    numbered = [SymbolPin("1", "1"), SymbolPin("2", "2")]
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "DIO-SMD_L2.8-W1.8", JLC_SMF, numbered
    )
    assert verdict.status == "unknown"
    assert verdict.rotation is None
    assert "polarity unknown" in verdict.note_text


def test_vertical_jlc_drawing_uses_the_label_when_the_token_cannot_apply():
    """A left/right token cannot be read off a vertical drawing; the label still can."""
    vertical = [Pad("1", 0.0, -1.45, 1.4, 1.3), Pad("2", 0.0, 1.45, 1.4, 1.3)]
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-FD", vertical, D2_PINS
    )
    assert verdict.rotation == 270  # cathode (pad 2) at the bottom of JLC's drawing
    assert any("vertical" in note for note in verdict.notes)
    # The token was noted, not applied, so it cannot accuse the footprint.
    assert verdict.confidence == "high"
    assert "drawn non-standard" not in verdict.note_text
    cathode_on_top = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", vertical, D1_PINS
    )
    assert cathode_on_top.rotation == 90


def test_bidirectional_token_aligns_by_axis():
    """A -BI TVS has no preferred orientation and resolves like a resistor."""
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-BI", JLC_SMF, []
    )
    assert (verdict.rotation, verdict.method, verdict.status) == (0, "axis", "green")
    assert any("bidirectional" in note for note in verdict.notes)


def test_axis_alignment_for_a_resistor_drawn_vertical():
    """Non-polar parts only need the axis: a vertical JLC drawing is 90, the same drawing 0."""
    kicad = [Pad("1", -0.7875, 0, 0.875, 0.95), Pad("2", 0.7875, 0, 0.875, 0.95)]
    vertical = [
        Pad("1", 0, -0.7875, 0.875, 0.95, 90.0),
        Pad("2", 0, 0.7875, 0.875, 0.95, 90.0),
    ]
    verdict = resolve(
        kicad, "Resistor_SMD:R_0603_1608Metric", "ok", "R0603", vertical, []
    )
    assert (verdict.rotation, verdict.method, verdict.status, verdict.fit) == (
        90,
        "axis",
        "green",
        "fits",
    )
    same = resolve(kicad, "Resistor_SMD:R_0603_1608Metric", "ok", "R0603", kicad, [])
    assert same.rotation == 0


def test_0402_pads_under_an_0603_part_are_tight():
    """JLC's pad centres fall outside the inner 80 percent of the KiCad pads: yellow, tight."""
    kicad_0402 = [Pad("1", -0.51, 0, 0.54, 0.64), Pad("2", 0.51, 0, 0.54, 0.64)]
    jlc_0603 = [Pad("1", -0.7875, 0, 0.875, 0.95), Pad("2", 0.7875, 0, 0.875, 0.95)]
    verdict = resolve(
        kicad_0402, "Resistor_SMD:R_0402_1005Metric", "ok", "R0603", jlc_0603, []
    )
    assert (verdict.status, verdict.fit) == ("yellow", "fits_tight")
    assert verdict.rotation == 0


def test_led_family_with_plus_minus_labels_and_no_kicad_evidence_is_a_diode():
    """An LED package whose symbol says +/- on a custom-named footprint without functions.

    Read as a capacitor it would assume KiCad pad 1 = + and give 180 on a part that
    sits at 0; the package family outranks the symbol labels.
    """
    plain = [Pad("1", -1.05, 0.0, 1.0, 1.2), Pad("2", 1.05, 0.0, 1.0, 1.2)]
    jlc = [Pad("1", -1.05, 0.0, 1.0, 1.2), Pad("2", 1.05, 0.0, 1.0, 1.2)]
    plus_minus = [SymbolPin("1", "-"), SymbolPin("2", "+")]
    verdict = resolve(plain, "Custom:LED0805-RD", "ok", "LED0805-RD", jlc, plus_minus)
    assert (verdict.rotation, verdict.status, verdict.polarity_light) == (
        0,
        "green",
        "green",
    )
    assert "assumed KiCad pad 1 = K" in verdict.note_text


def test_pin1_light_reads_the_other_pads_function():
    """Pad 1 unnamed and pad 2 = K: pad 1 is the anode, so JLC's K-on-pin-1 lands on it (yellow)."""
    half = [Pad("1", -1.45, 0.0, 1.3, 1.4), Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "K")]
    verdict = resolve(
        half, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert (verdict.rotation, verdict.polarity_light, verdict.status) == (
        180,
        "yellow",
        "yellow",
    )


def test_switch_a_b_functions_do_not_make_a_diode():
    """KiCad's SW_SPST names its pins A and B; the part stays non-polar and resolves by axis."""
    switch = [
        Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "A"),
        Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "B"),
    ]
    numbered = [SymbolPin("1", "1"), SymbolPin("2", "2")]
    verdict = resolve(
        switch, "Switch:SW_SPST", "ok", "SW-SMD_2P-L3.0-W2.0", JLC_SMF, numbered
    )
    assert (verdict.method, verdict.rotation, verdict.status) == ("axis", 0, "green")


def test_pos_neg_functions_name_a_capacitor():
    """POS/NEG are capacitor words on both sides: polar_cap, FD is 0 and the name agrees."""
    cap = [Pad("1", -2.7, 0, 3.5, 1.6, 0, "POS"), Pad("2", 2.7, 0, 3.5, 1.6, 0, "NEG")]
    jlc = [Pad("1", -2.7, 0, 3.5, 1.6), Pad("2", 2.7, 0, 3.5, 1.6)]
    verdict = resolve(cap, "Custom:Cap", "ok", "CAP-SMD_BD6.3-L6.6-W6.6-FD", jlc, [])
    assert (verdict.rotation, verdict.name_rotation, verdict.confidence) == (
        0,
        0,
        "high",
    )


def test_non_finite_pad_rotation_is_unknown():
    """A pad angle of infinity or NaN short-circuits like a bad coordinate would."""
    for bad in (float("inf"), float("nan")):
        kicad = [KICAD_SMF[0]._replace(rotation=bad), KICAD_SMF[1]]
        verdict = resolve(
            kicad, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
        )
        assert (verdict.status, verdict.rotation) == ("unknown", None)
        jlc = [JLC_SMF[0]._replace(rotation=bad), JLC_SMF[1]]
        verdict = resolve(
            KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", jlc, D1_PINS
        )
        assert (verdict.status, verdict.rotation) == ("unknown", None)


def test_three_pad_footprint_pitch_failure_is_reported_as_pitch():
    """The note follows the fit decision: a two-terminal part missing its pads is pitch, not count."""
    kicad = [
        Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "K"),
        Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "A"),
        Pad("3", 0.0, 1.5, 1.0, 0.5),
    ]
    wide = [Pad("1", -2.6, 0.0, 1.3, 1.4), Pad("2", 2.6, 0.0, 1.3, 1.4)]
    verdict = resolve(
        kicad, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", wide, D1_PINS
    )
    assert (verdict.status, verdict.fit) == ("red", "pitch")
    assert "does not fit: pitch" in verdict.note_text


def test_polarized_result_contradicting_the_name_is_medium_with_a_note():
    """Anode on KiCad pad 1 gives 180 where the name says 0: right, but flagged.

    The note names the schematic's pin assignment as the cause, not the drawing.
    """
    wired_backwards = [
        Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "A"),
        Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "K"),
    ]
    verdict = resolve(
        wired_backwards,
        "Diode_SMD:D_SMF",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        JLC_SMF,
        D1_PINS,
    )
    assert (verdict.rotation, verdict.confidence) == (180, "medium")
    assert (
        "the schematic puts the cathode on KiCad pad 2; the name's 0° assumes pad 1"
        in verdict.note_text
    )
    assert "drawn non-standard" not in verdict.note_text


def test_a_footprint_drawn_cathode_right_is_named_non_standard():
    """Pad 1 is the cathode, but drawn on the right: the name's 0 assumes it on the left."""
    cathode_right = [
        Pad("1", 1.45, 0.0, 1.3, 1.4, 0.0, "K"),
        Pad("2", -1.45, 0.0, 1.3, 1.4, 0.0, "A"),
    ]
    verdict = resolve(
        cathode_right,
        "Custom:D_SMF_Mirrored",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        JLC_SMF,
        D1_PINS,
    )
    assert (verdict.rotation, verdict.confidence) == (180, "medium")
    assert "KiCad footprint drawn non-standard; name says 0°" in verdict.note_text


def test_coincident_jlc_pads_are_degenerate_not_vertical():
    """Two JLC pads at one point: no axis, no token side, and the note says degenerate."""
    coincident = [Pad("1", 0.0, 0.0, 1.3, 1.4), Pad("2", 0.0, 0.0, 1.3, 1.4)]
    verdict = resolve(
        KICAD_SMF,
        "Diode_SMD:D_SMF",
        "ok",
        "SMF_L2.8-W1.8-LS3.7-RD",
        coincident,
        D1_PINS,
    )
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert verdict.note_text == "pad geometry is degenerate"


def test_bidirectional_tvs_with_d_tvs_pin_names_aligns_by_axis():
    """KiCad's D_TVS symbol names both pins A1/A2; a -BI part must not read them as two anodes."""
    tvs = [
        Pad("1", -1.45, 0.0, 1.3, 1.4, 0.0, "A1"),
        Pad("2", 1.45, 0.0, 1.3, 1.4, 0.0, "A2"),
    ]
    verdict = resolve(
        tvs, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-BI", JLC_SMF, D1_PINS
    )
    assert (verdict.method, verdict.rotation, verdict.status) == ("axis", 0, "green")
    assert "bidirectional" in verdict.note_text
    polarized = resolve(
        tvs, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert polarized.status == "unknown"
    assert "contradictory" in polarized.note_text


def test_non_polar_flag_marks_axis_and_marked_parts_only():
    """Resistors and marked inductors may sit either way round; diodes may not."""
    resistor = [Pad("1", -0.7875, 0, 0.875, 0.95), Pad("2", 0.7875, 0, 0.875, 0.95)]
    assert resolve(
        resistor, "Resistor_SMD:R_0603_1608Metric", "ok", "R0603", resistor, []
    ).non_polar
    marked = resolve(
        resistor, "Inductor_SMD:L_0603_1608Metric", "ok", "L0603-RD", resistor, []
    )
    assert marked.non_polar and marked.method == "polarity"
    diode = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D1_PINS
    )
    assert not diode.non_polar


def test_token_places_the_band_which_is_the_cathode_or_the_negative_end():
    """FD puts the band right: the cathode on diodes and the negative end on capacitors, molded chips included."""
    assert token_reference_side("CAP-SMD_L3.2-W1.6-RD-C7171", "positive") == "right"
    assert token_reference_side("CAP-SMD_L3.2-W1.6-FD", "positive") == "left"
    assert token_reference_side("CAP-SMD_L3.5-W2.8-R-RD", "positive") == "right"
    assert token_reference_side("CAP-SMD_BD6.3-L6.6-W6.6-FD", "positive") == "left"
    assert token_reference_side("CAP-SMD_BD6.3-L6.6-W6.6-FD", "negative") == "right"
    assert token_reference_side("SMF_L2.8-W1.8-LS3.7-FD-1", "cathode") == "right"
    assert token_reference_side("SMF_L2.8-W1.8-LS3.7-FD-1", "anode") == "left"


def test_a_symbol_label_deciding_alone_is_high_confidence():
    """No token and no marks: the part's own symbol label decides the reference pad, at full strength."""
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "DIO-SMD_L2.8-W1.8", JLC_SMF, D2_PINS
    )
    assert (verdict.rotation, verdict.polarity_source, verdict.confidence) == (
        180,
        "label",
        "high",
    )


def test_a_symbol_label_against_the_name_token_is_a_tie():
    """A label is a full vote: against a token naming the other pad, with nothing else voting, the data is inconsistent."""
    verdict = resolve(
        KICAD_SMF, "Diode_SMD:D_SMF", "ok", "SMF_L2.8-W1.8-LS3.7-RD", JLC_SMF, D2_PINS
    )
    assert (verdict.status, verdict.rotation) == ("red", None)
    assert (
        "EasyEDA data inconsistent: name token and symbol pin-1 label disagree"
        in verdict.notes
    )


def test_c7171_marks_outvote_its_token_and_a_can_reads_the_same_token_plainly():
    """C5 on the corner-case board (C7171): its RD token says 180 but both + marks say 0, and JLC's preview agreed with the marks."""
    kicad = [Pad("1", -1.3525, 0, 1.4, 1.2), Pad("2", 1.3525, 0, 1.4, 1.2)]
    jlc = [Pad("1", -1.53, 0, 1.4, 1.2), Pad("2", 1.53, 0, 1.4, 1.2)]
    numbered = [SymbolPin("1", "1"), SymbolPin("2", "2")]
    token_alone = resolve(
        kicad,
        "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A",
        "ok",
        "CAP-SMD_L3.2-W1.6-RD-C7171",
        jlc,
        numbered,
    )
    assert (token_alone.rotation, token_alone.name_rotation) == (180, 180)
    assert token_alone.polarity_source == "token"
    marks = DrawingMarks(positive_pin="1", positive_pad="1")
    verdict = resolve(
        kicad,
        "Capacitor_Tantalum_SMD:CP_EIA-3216-18_Kemet-A",
        "ok",
        "CAP-SMD_L3.2-W1.6-RD-C7171",
        jlc,
        numbered,
        marks=marks,
    )
    assert (verdict.rotation, verdict.name_rotation, verdict.status) == (
        0,
        180,
        "green",
    )
    assert (verdict.polarity_source, verdict.confidence) == ("drawing", "medium")
    assert "assumed KiCad pad 1 = +" in verdict.note_text
    assert (
        "name token names the other pad; symbol + mark, footprint + mark decide"
        in verdict.note_text
    )
    assert "drawn non-standard" not in verdict.note_text
    can = resolve(
        kicad,
        "Capacitor_SMD:CP_Elec_4x5.4",
        "ok",
        "CAP-SMD_BD4.0-L4.3-W4.3-RD",
        jlc,
        numbered,
    )
    assert (can.rotation, can.name_rotation) == (180, 180)


def test_a_non_polar_pad_overlapping_by_less_than_half_misses():
    """MIN_OVERLAP: a JLC pad on less than half of its KiCad pad is red; on more, tight."""
    kicad = [Pad("1", -1.0, 0.0, 1.0, 1.0), Pad("2", 1.0, 0.0, 1.0, 1.0)]
    for offset, expected in (
        (0.55, ("red", "pitch", None)),
        (0.45, ("yellow", "fits_tight", 0)),
    ):
        jlc = [
            Pad("1", -1.0 - offset, 0.0, 1.0, 1.0),
            Pad("2", 1.0 + offset, 0.0, 1.0, 1.0),
        ]
        verdict = resolve(kicad, "Custom:Part", "ok", "X", jlc, [])
        assert (verdict.status, verdict.fit, verdict.rotation) == expected, offset


def test_a_part_with_one_named_pad_is_unknown():
    """One pad gives no alignment: unknown, no rotation, and the note says why."""
    pad = [Pad("1", 0.0, 0.0, 1.0, 1.0)]
    verdict = resolve(pad, "Custom:Part", "ok", "X", pad, [])
    assert (verdict.status, verdict.rotation) == ("unknown", None)
    assert verdict.notes == [SINGLE_PAD_NOTE]


def test_a_token_on_a_non_polar_part_drawn_upright_leaves_it_to_its_axis():
    """A marked inductor whose JLC pads are drawn upright: the token cannot be applied.

    The part is aligned by its axis, as it would be without the token, rather than
    left unknown for a polarity it does not have.
    """
    kicad = [Pad("1", -0.5, 0.0, 0.6, 0.6), Pad("2", 0.5, 0.0, 0.6, 0.6)]
    upright = [Pad("1", 0.0, -0.5, 0.6, 0.6), Pad("2", 0.0, 0.5, 0.6, 0.6)]
    pins = [SymbolPin("1", "1"), SymbolPin("2", "2")]
    marked = resolve(kicad, "Custom:L", "ok", "L0402-RD", upright, pins)
    plain = resolve(kicad, "Custom:L", "ok", "L0402", upright, pins)
    assert (marked.status, marked.method, marked.rotation) == ("green", "axis", 90)
    assert (marked.status, marked.method, marked.rotation) == (
        plain.status,
        plain.method,
        plain.rotation,
    )
    assert NO_VOTE_NOTE not in marked.notes
    assert NON_POLAR_TOKEN_NOTE not in marked.notes


def test_labels_on_a_non_polar_part_do_not_vote_against_its_token():
    """A switch's A/B or a transistor's C/E on a tokened part: only the token votes.

    Read as a label vote, A/B tied with the token (red) and C/E's collector C read
    as a cathode turned the pin-1 light yellow.
    """
    pads = [Pad("1", -1.0, 0.0, 0.8, 0.8), Pad("2", 1.0, 0.0, 0.8, 0.8)]
    for labels in (("A", "B"), ("C", "E")):
        pins = [SymbolPin("1", labels[0]), SymbolPin("2", labels[1])]
        for package in ("FOO-SMD_L1.6-W0.8-RD", "FOO-SMD_L1.6-W0.8-FD"):
            verdict = resolve(pads, "Lib:Two", "ok", package, pads, pins)
            assert (verdict.status, verdict.method) == ("green", "polarity"), (
                labels,
                package,
            )
            assert YELLOW_NOTE not in verdict.notes


def test_a_note_lists_its_pads_by_number_whatever_their_order():
    """The pads a note names come in number order, so a report does not follow the listing."""
    kicad = [Pad("2", 1.0, 0.0, 1.0, 1.0), Pad("1", -1.0, 0.0, 1.0, 1.0)]
    jlc = [Pad("2", 1.45, 0.0, 1.0, 1.0), Pad("1", -1.45, 0.0, 1.0, 1.0)]
    verdict = resolve(kicad, "Custom:Part", "ok", "X", jlc, [])
    assert "JLC pads 1, 2 sit at the edge of your pads" in verdict.note_text
