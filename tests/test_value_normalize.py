"""Unit tests for canonical catalog spellings of passive values."""

from __future__ import annotations

import pytest

from value_normalize import (
    CAPACITANCE,
    INDUCTANCE,
    RESISTANCE,
    canonicalize,
    fold_signs,
    quantity_for_reference,
    whole_value,
)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("R1", RESISTANCE),
        ("R100", RESISTANCE),
        ("r1", RESISTANCE),
        ("RN3", RESISTANCE),
        ("RV1", RESISTANCE),
        ("TH1", RESISTANCE),
        ("C1", CAPACITANCE),
        ("CN2", CAPACITANCE),
        ("L1", INDUCTANCE),
        ("R1A", RESISTANCE),
        ("R?", RESISTANCE),
        # A separator ends the prefix run rather than disqualifying it.
        ("R_SHUNT1", RESISTANCE),
        ("C_IN1", CAPACITANCE),
        ("L.TEST1", INDUCTANCE),
        ("C-1", CAPACITANCE),
    ],
)
def test_reference_prefixes_that_name_a_passive(reference, expected):
    """The prefixes KiCad's libraries give passives resolve to their quantity."""
    assert quantity_for_reference(reference) is expected


@pytest.mark.parametrize(
    "reference",
    [
        "LD1",  # laser diode, not an inductor
        "LS1",  # speaker, not an inductor
        "CB1",  # circuit breaker, not a capacitor
        "D1",
        "Q1",
        "U1",
        "J1",
        "SW1",
        "Y1",
        "FL1",
        "MK1",
        "K1",
        "MYPART1",
        "CONN-1",
        "#PWR03",
        "#FLG01",
        "",
        "1",
        "?",
    ],
)
def test_reference_prefixes_that_do_not(reference):
    """Prefixes are matched whole, so LD, LS and CB are not L, L and C.

    An unrecognised prefix must resolve to nothing rather than to a guess, so a
    custom library's own designators leave their values alone.
    """
    assert quantity_for_reference(reference) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.1k", "100Ω"),
        ("0.1kΩ", "100Ω"),
        (".1k", "100Ω"),
        ("0.47k", "470Ω"),
        ("0.1M", "100kΩ"),
        ("0.1MΩ", "100kΩ"),
        ("0.001M", "1kΩ"),
        ("10k", "10kΩ"),
        ("10K", "10kΩ"),
        ("1M", "1MΩ"),
        ("100", "100Ω"),
        ("100R", "100Ω"),
        ("100r", "100Ω"),
        ("100ohm", "100Ω"),
        ("100ohms", "100Ω"),
        ("100Ω", "100Ω"),
        ("4K7", "4.7kΩ"),
        ("4k7", "4.7kΩ"),
        ("1k5", "1.5kΩ"),
        ("1M5", "1.5MΩ"),
        ("4R7", "4.7Ω"),
        ("2R2", "2.2Ω"),
        ("390R", "390Ω"),
        # 'o' was an ohm alias in the prefill this replaces, which stripped a
        # trailing R, r or o before appending Ω.
        ("100o", "100Ω"),
        ("100O", "100Ω"),
        ("100ko", "100kΩ"),
        ("0.1o", "100mΩ"),
        # RKM permits the leading zero to be dropped for a sub-unit value.
        ("R47", "470mΩ"),
        ("r47", "470mΩ"),
        ("R5", "500mΩ"),
        ("R05", "50mΩ"),
        ("390r", "390Ω"),
        ("1R5", "1.5Ω"),
        ("1R0", "1Ω"),
        ("12R3", "12.3Ω"),
        # Below an ohm the ladder drops to milliohms, which is also where the
        # catalog switches: 500mΩ has 257 parts against 84 for 0.5Ω, and
        # 0.05Ω has none at all while 50mΩ has 844.
        ("0R5", "500mΩ"),
        ("0R47", "470mΩ"),
        ("0R05", "50mΩ"),
        ("4k7Ω", "4.7kΩ"),
        ("0.1", "100mΩ"),
        ("0.05", "50mΩ"),
        ("0.001", "1mΩ"),
        ("0.0001k", "100mΩ"),
    ],
)
def test_resistance_values(value, expected):
    """Every spelling a schematic uses for a resistance lands on the catalog's."""
    assert canonicalize(value, RESISTANCE) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.1u", "100nF"),
        ("0.1uF", "100nF"),
        ("0.1uf", "100nF"),
        ("0.1µF", "100nF"),
        ("0.1μF", "100nF"),
        (".1u", "100nF"),
        ("0.01u", "10nF"),
        ("0.001u", "1nF"),
        ("0.0001u", "100pF"),
        ("0.47uF", "470nF"),
        ("0.022uF", "22nF"),
        ("100n", "100nF"),
        ("100nF", "100nF"),
        ("100p", "100pF"),
        ("100pF", "100pF"),
        ("10u", "10uF"),
        ("10µF", "10uF"),
        ("4700u", "4700uF"),
        ("2u2", "2.2uF"),
        ("4n7", "4.7nF"),
        ("4p7", "4.7pF"),
        ("0.1mF", "100uF"),
        ("1u0", "1uF"),
    ],
)
def test_capacitance_values(value, expected):
    """Likewise for capacitance, including both micro signs."""
    assert canonicalize(value, CAPACITANCE) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.1uH", "100nH"),
        ("0.1u", "100nH"),
        ("10uH", "10uH"),
        ("10u", "10uH"),
        ("2u2", "2.2uH"),
        ("4n7", "4.7nH"),
        ("0.1mH", "100uH"),
        ("100n", "100nH"),
    ],
)
def test_inductance_values(value, expected):
    """And for inductance, which shares the capacitor's prefixes."""
    assert canonicalize(value, INDUCTANCE) == expected


def test_the_reference_designator_settles_an_ambiguous_prefix():
    """A bare 'u' is microfarads or microhenries; only the refdes knows which.

    This is the whole reason canonicalization is driven by the reference rather
    than guessed from the value.
    """
    assert canonicalize("2u2", CAPACITANCE) == "2.2uF"
    assert canonicalize("2u2", INDUCTANCE) == "2.2uH"
    assert canonicalize("0.1u", CAPACITANCE) == "100nF"
    assert canonicalize("0.1u", INDUCTANCE) == "100nH"


@pytest.mark.parametrize(
    ("value", "quantity"),
    [
        ("100nF", RESISTANCE),
        ("4k7", CAPACITANCE),
        ("10uH", CAPACITANCE),
        ("10uF", INDUCTANCE),
        ("1M5", CAPACITANCE),
        ("100Ω", CAPACITANCE),
    ],
)
def test_a_spelled_out_unit_beats_the_reference_designator(value, quantity):
    """A value that names its own quantity is never rewritten into another.

    If the board says a part marked C1 is 4k7, that is a mistake worth seeing,
    not something to reinterpret as 4.7 kilofarads.
    """
    assert canonicalize(value, quantity) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.5mΩ", "0.5mΩ"),
        ("0.1mΩ", "0.1mΩ"),
        ("0.2mohm", "0.2mΩ"),
        ("0.75mR", "0.75mΩ"),
        ("1mΩ", "1mΩ"),
        ("4m7", "4.7mΩ"),
        ("100mΩ", "100mΩ"),
    ],
)
def test_milli_is_not_mega(value, expected):
    """A milliohm shunt stays on the milli rung; it never lands on mega or kilo.

    Folding the prefix case would rewrite 0.5mΩ as 500kΩ -- nine orders of
    magnitude out, burying the current-sense resistors the user wanted.  What
    prevents it is that `prefixes` is keyed by the exact character, so m and M
    select different rungs.
    """
    assert canonicalize(value, RESISTANCE) == expected


def test_mega_still_goes_to_mega():
    """The counterpart: an upper-case M is mega on the same ladder."""
    assert canonicalize("0.5MΩ", RESISTANCE) == "500kΩ"
    assert canonicalize("4M7", RESISTANCE) == "4.7MΩ"
    assert canonicalize("0.1M", RESISTANCE) == "100kΩ"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1meg", "1MΩ"),
        ("1Meg", "1MΩ"),
        ("1MEG", "1MΩ"),
        ("1mEg", "1MΩ"),
        ("1MeG", "1MΩ"),
        ("4.7meg", "4.7MΩ"),
        ("2.2Meg", "2.2MΩ"),
        ("0.47meg", "470kΩ"),
        ("1 meg", "1MΩ"),
        ("1megohm", "1MΩ"),
        ("1MegΩ", "1MΩ"),
        ("10MEGOHMS", "10MΩ"),
    ],
)
def test_spice_meg_is_mega(value, expected):
    """SPICE spells mega 'meg', because its own M means milli, in any case.

    The catalog never does: no resistor description in a 717,025-part snapshot
    contains 'meg', so a 1meg passed through untouched finds nothing at all.
    """
    assert canonicalize(value, RESISTANCE) == expected


def test_meg_has_no_rkm_form():
    """RKM puts a single letter where the decimal point goes, never meg.

    SPICE, where meg comes from, has no RKM notation at all; in RKM 4.7
    megohms is 4M7, which already canonicalizes.
    """
    assert canonicalize("4meg7", RESISTANCE) is None


def test_the_catalog_spells_small_resistances_in_milliohms():
    """0.05Ω appears nowhere in the catalog; 50mΩ is how it is written.

    Counted over the Resistors category of a 717,025-part snapshot: 10,127 parts
    carry a milliohm spelling across 189 distinct values, 523 of them '100mΩ',
    against 93 for '0.1Ω' and none at all for '0.05Ω'.
    """
    assert canonicalize("0.1", RESISTANCE) == "100mΩ"
    assert canonicalize("0.05", RESISTANCE) == "50mΩ"
    assert canonicalize("0.1Ω", RESISTANCE) == "100mΩ"


def test_milli_is_the_bottom_of_the_resistance_ladder():
    """Nothing is spelled in microohms, so sub-milliohm parts stay in milliohms.

    The catalog has no uΩ/µΩ description at all, and writes 337 parts as 0.xmΩ
    -- 141 of them 0.5mΩ.  Scaling those down would invent a spelling.
    """
    assert canonicalize("0.5mΩ", RESISTANCE) == "0.5mΩ"
    assert canonicalize("0.05mΩ", RESISTANCE) == "0.05mΩ"
    assert canonicalize("0.001", RESISTANCE) == "1mΩ"


@pytest.mark.parametrize(
    "value", ["0.1MF", "0.1MH", "1M5", "100M", "1meg", "1Meg", "1MEG", "1mEg"]
)
def test_mega_is_not_milli(value):
    """And the reactive quantities have no mega rung."""
    assert canonicalize(value, CAPACITANCE) is None
    assert canonicalize(value, INDUCTANCE) is None


@pytest.mark.parametrize(
    "value",
    [
        "1N4148",
        "1N914",
        "1N34",
        "1N60",
        "2N3904",
        "2N5088",
        "LM358",
        "NE555",
        "ESP32",
        "STM32F103",
        "X7R",
        "C0G",
        "NP0",
        "0603",
        "DNP",
        "NO/NC",
        "16MHz",
        "",
        "   ",
        None,
    ],
)
def test_values_that_are_not_values(value):
    """Part numbers, dielectric codes, packages and markings are left alone."""
    assert canonicalize(value, RESISTANCE) is None
    assert canonicalize(value, CAPACITANCE) is None


@pytest.mark.parametrize(
    "value", ["0Ω", "0R", "0r", "0", "0.0", "0ohm", "0k", "0kΩ", "0mΩ", "0M", "0o"]
)
def test_zero_ohm_jumpers(value):
    """A zero-ohm jumper is a stocked part, and the catalog spells it 0Ω.

    It must not be scaled onto the milli rung as 0mΩ, must not keep a prefix it
    arrived with (0kΩ), and must not be left as a bare '0', which matches most
    of the catalog.
    """
    assert canonicalize(value, RESISTANCE) == "0Ω"


@pytest.mark.parametrize("value", ["-1k", "-0.1", "- 1k", "+1k"])
def test_signed_values_are_not_values(value):
    """A sign is not part of any spelling the catalog uses."""
    assert canonicalize(value, RESISTANCE) is None


def test_zero_has_no_meaning_for_the_reactive_quantities():
    """Only resistance has a base rung, so a bare 0 means nothing elsewhere."""
    assert canonicalize("0", CAPACITANCE) is None
    assert canonicalize("0", INDUCTANCE) is None


@pytest.mark.parametrize("value", ["M3", "K47", "M47", "k5", "u47", "n47", "p47"])
def test_an_omitted_leading_zero_is_only_read_for_the_base_unit(value):
    """R47 is 0.47Ω, but M3 is a mounting hole, not 0.3MΩ.

    IEC 60062 documents the omitted integer only for the base-unit letter: its
    examples are R47, 4R7, 4K7 and 4M7, so a prefix letter always follows a
    digit.  Restricting it the same way keeps screw sizes and part markings from
    parsing as values.  The standard makes the same point from the other side --
    an RKM code starting with R that could be read as a reference designator
    should be written 0R47 instead.
    """
    assert canonicalize(value, RESISTANCE) is None


@pytest.mark.parametrize("value", ["R47", "R5", "u47", "p47", "n47"])
def test_the_omitted_leading_zero_needs_a_base_rung(value):
    """Capacitance and inductance have no base rung, so the form means nothing."""
    assert canonicalize(value, CAPACITANCE) is None
    assert canonicalize(value, INDUCTANCE) is None


def test_no_quantity_means_no_opinion():
    """With no quantity there is nothing to canonicalize against."""
    assert canonicalize("0.1uF", None) is None
    assert canonicalize("4k7", None) is None


def test_values_scale_up_to_the_rung_the_catalog_prints():
    """A value reaching a thousand moves up a rung, except where the catalog will not.

    Each quantity stops somewhere different, and the catalog says where.
    Counting values on each rung that are >= 1000 -- i.e. times the catalog
    declined to scale up -- resistance's k rung has 0 and capacitance's u rung
    has 2,848, because electrolytics are called 4700uF and never 4.7mF.
    """
    # Resistance always scales: 1000Ω, 2200Ω, 4700Ω and 10000Ω have no catalog
    # parts at all, against 5901, 719, 879 and 2576 for the kΩ spellings.
    assert canonicalize("2200", RESISTANCE) == "2.2kΩ"
    assert canonicalize("10000", RESISTANCE) == "10kΩ"
    assert canonicalize("1000mΩ", RESISTANCE) == "1Ω"
    assert canonicalize("2200000", RESISTANCE) == "2.2MΩ"
    # Capacitance stops at micro.
    assert canonicalize("4700u", CAPACITANCE) == "4700uF"
    assert canonicalize("4700uF", CAPACITANCE) == "4700uF"
    assert canonicalize("1000n", CAPACITANCE) == "1uF"
    assert canonicalize("1000p", CAPACITANCE) == "1nF"
    # Inductance goes on to milli.
    assert canonicalize("1000u", INDUCTANCE) == "1mH"
    assert canonicalize("1000n", INDUCTANCE) == "1uH"


def test_a_rung_above_the_ceiling_is_brought_down():
    """Milli is an input spelling for capacitance, never an output one.

    The catalog has no mF description at all: 1mF, 2mF, 10mF and 4.7mF have zero
    parts, against 1339, 22, 54 and 145 for the uF spellings. So a value that
    arrives on the milli rung must come down to micro regardless of magnitude,
    not only when its mantissa is below one.
    """
    assert canonicalize("1mF", CAPACITANCE) == "1000uF"
    assert canonicalize("2mF", CAPACITANCE) == "2000uF"
    assert canonicalize("10mF", CAPACITANCE) == "10000uF"
    assert canonicalize("4.7mF", CAPACITANCE) == "4700uF"
    assert canonicalize("0.1mF", CAPACITANCE) == "100uF"
    assert canonicalize("1m", CAPACITANCE) == "1000uF"
    # Inductance prints milli, so it stays.
    assert canonicalize("1mH", INDUCTANCE) == "1mH"
    assert canonicalize("0.1mH", INDUCTANCE) == "100uH"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0R001", "1mΩ"),
        ("R001", "1mΩ"),
        ("0R0005", "0.5mΩ"),
        ("0R00025", "0.25mΩ"),
    ],
)
def test_rkm_fractions_are_not_limited_to_two_digits(value, expected):
    """A milliohm written in RKM needs three or more fractional digits.

    Part numbers are kept out by looking the letter up in the quantity's own
    table -- N is not an RKM prefix for any of them -- so capping the digits
    bought nothing and cost these spellings.
    """
    assert canonicalize(value, RESISTANCE) == expected


@pytest.mark.parametrize(
    "value",
    [
        "1N4148",
        "1N914",
        "1N34",
        "1N60",
        "1N5817",
        "2N3904",
        "2N5088",
        "2SC1815",
        "BC847",
        "1206W4F1001T5E",
    ],
)
def test_part_numbers_stay_out_without_the_digit_cap(value):
    """The quantity-specific letter table is what excludes them, on its own."""
    assert canonicalize(value, RESISTANCE) is None
    assert canonicalize(value, CAPACITANCE) is None
    assert canonicalize(value, INDUCTANCE) is None


def test_each_ladder_stops_at_the_bottom_rung_the_catalog_uses():
    """The floor is evidence, like the ceiling, not the end of the tuple.

    No inductor description uses pH at all -- zero of 43,980 -- while 110 are
    written 0.xnH, so scaling an inductance below nano finds nothing where
    leaving it alone finds parts: 0.6nH has 31, 600pH has none.  Capacitance
    does use pF (12,096 values) and resistance does use mΩ, so those floors sit
    lower.
    """
    assert canonicalize("0.6nH", INDUCTANCE) == "0.6nH"
    assert canonicalize("0.1nH", INDUCTANCE) == "0.1nH"
    assert canonicalize("0.5nH", INDUCTANCE) == "0.5nH"
    # Capacitance and resistance keep their lower floors.
    assert canonicalize("0.5nF", CAPACITANCE) == "500pF"
    assert canonicalize("0.001", RESISTANCE) == "1mΩ"


def test_a_rung_below_the_floor_is_brought_up():
    """The mirror of the ceiling case: 100pH is not a spelling any part uses."""
    assert canonicalize("100pH", INDUCTANCE) == "0.1nH"
    assert canonicalize("470pH", INDUCTANCE) == "0.47nH"
    # pF is a real capacitor spelling, so it is left where it is.
    assert canonicalize("100pF", CAPACITANCE) == "100pF"


def test_scaling_is_idempotent_at_every_rung():
    """Canonicalizing the output again must not move it."""
    for value, quantity in [
        ("2200", RESISTANCE),
        ("0.1", RESISTANCE),
        ("1000n", CAPACITANCE),
        ("4700u", CAPACITANCE),
        ("1000u", INDUCTANCE),
        ("0.6nH", INDUCTANCE),
        ("100pH", INDUCTANCE),
    ]:
        once = canonicalize(value, quantity)
        assert canonicalize(once, quantity) == once


def test_a_value_already_in_catalog_form_is_returned_unchanged():
    """Canonicalizing twice is the same as canonicalizing once."""
    for value, quantity in [
        ("100nF", CAPACITANCE),
        ("4.7kΩ", RESISTANCE),
        ("10uH", INDUCTANCE),
        ("100Ω", RESISTANCE),
    ]:
        once = canonicalize(value, quantity)
        assert once == value
        assert canonicalize(once, quantity) == once


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        # Case is the whole meaning of an m or M on a resistance (issue #849).
        ("10mΩ", "10mΩ"),
        ("10MΩ", "10MΩ"),
        ("1.5mΩ", "1.5mΩ"),
        ("0.5mΩ", "0.5mΩ"),
        ("2.2MΩ", "2.2MΩ"),
        # U+2126 OHM SIGN is spelled as the Greek capital omega the catalog
        # writes: no description in a 717,025-part snapshot uses U+2126.
        ("10m\u2126", "10mΩ"),
        ("1kΩ", "1kΩ"),
        ("4.7kΩ", "4.7kΩ"),
        ("100Ω", "100Ω"),
        ("1Ω", "1Ω"),
        ("100nF", "100nF"),
        ("10uF", "10uF"),
        ("22pF", "22pF"),
        ("4.7uH", "4.7uH"),
        ("10mH", "10mH"),
        # Supercapacitors are written in whole farads.
        ("1F", "1F"),
        # The catalog writes the zero before a point, and the search refuses
        # a digit or a point in front of the value, so it has to be there.
        (".1uF", "0.1uF"),
        (".47Ω", "0.47Ω"),
        (".5mΩ", "0.5mΩ"),
        # Where case means nothing, the catalog's is used: after a digit it
        # writes no KΩ, UF, NF, PF, UH or NH at all.
        ("1KΩ", "1kΩ"),
        ("100NF", "100nF"),
        ("10UF", "10uF"),
        ("22PF", "22pF"),
        ("4.7UH", "4.7uH"),
        ("100nf", "100nF"),
        ("100nh", "100nH"),
        # Micro signs are read as the catalog's u.
        ("10\u00b5F", "10uF"),
        ("4.7\u03bcH", "4.7uH"),
    ],
)
def test_a_value_written_with_its_unit_is_matched_whole(term, expected):
    """1kΩ is one value, so the search can refuse the 5.1kΩ that contains it."""
    assert whole_value(term) == expected


@pytest.mark.parametrize(
    "term",
    [
        # No unit: 10m could be a length, a current, or a megohm written in lower
        # case, and 1k is part of 1kHz and 1kV as well as 1kΩ.
        "10m",
        "10M",
        "1k",
        "100n",
        # Not how the catalog writes a value.
        "4k7",
        "1kR",
        "10kohm",
        "10mohm",
        "10ω",
        # A prefix the quantity has no rung for: there are no megahenries or
        # microohms, and 1MF is an old way of writing microfarads.
        "10MH",
        "1MF",
        "10uΩ",
        # Another quantity, or not a value at all.
        "10MHz",
        "10mA",
        "50V",
        "2N3904",
        "0603",
        # Only ASCII digits: a fullwidth or Arabic-Indic one never appears in
        # the catalog's values.
        "\uff11k\u03a9",
        "\u0663k\u03a9",
        # Nothing but digits, a point, a prefix and a unit ever reaches a GLOB
        # pattern, so none of * ? [ can.
        "1*mΩ",
        "1?MΩ",
        "[1]mΩ",
        "mΩ",
        "kΩ",
        "10mΩ5",
        "x10mΩ",
        "1FF",
        "",
    ],
)
def test_anything_else_is_left_to_the_substring_search(term):
    """Only a number, an optional prefix and Ω, F or H is matched whole."""
    assert whole_value(term) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10\u00b5F", "10uF"),
        ("10\u03bcF", "10uF"),
        ("4.7\u00b5", "4.7u"),
        ("10uF", "10uF"),
        ("LM358", "LM358"),
        ("5\u2126", "5\u03a9"),
        ("10m\u2126", "10m\u03a9"),
        ("10MΩ", "10MΩ"),
    ],
)
def test_micro_and_ohm_signs_are_spelled_as_the_catalog_writes_them(text, expected):
    """Both micro signs are written u, and the ohm sign as the Greek omega."""
    assert fold_signs(text) == expected
