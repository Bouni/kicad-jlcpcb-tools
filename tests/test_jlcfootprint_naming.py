"""Tests for jlcfootprint.naming: family extraction and the (family, token) rotation table."""

import pytest

from jlcfootprint.naming import (
    CATHODE_PIN1_FAMILIES,
    extract_family,
    extract_orientation_tokens,
    parse_package_name,
)


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("C0603", "C"),
        ("LED0603-RD", "LED"),
        ("SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL", "SOIC-8"),
        ("CAP-SMD_BD6.3-L6.6-W6.6-FD", "CAP-SMD"),
        ("SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR", "SOT-23-3"),
        ("SMF-L2.9-W1.9-LS3.9-FD", "SMF"),
        ("SMA-L4.3-W2.6-LS5.3-BI", "SMA"),
        ("LED-SMD-L1.6-W1.6-FD", "LED-SMD"),
        ("DO-27-BD5.3-L9.0-P14.0-D1.3-R-RD", "DO-27"),
        ("D-L1.6-W0.8-RD", "D"),
        ("TO-220-7-L10.2-W4.5-P1.27-BL", "TO-220-7"),
        ("CONN-SMD-2P-P1.20_BM02B-ACHSS-GAN-TF-LF-SN", "CONN-SMD-2P"),
        ("L0603", "L"),
        ("L0201-RD", "L"),
        ("R0603_1", "R"),
        ("L0402-SMD_L1.0-W0.5-MLK1005S7N5JTD25", "L"),
        ("LED0603_L1.6-W0.8-RD_CSL0901S10T1", "LED"),
        ("D2PAK-7_L10.2-W9.0-P1.27-LS15.2-BL", "D2PAK-7"),
        ("RJ45-TH_0826-1X1T-23-F", "RJ45-TH"),
    ],
)
def test_extract_family(name, family):
    """The family is everything before the underscore, or the leading letters, cut at a dimension."""
    assert extract_family(name) == family


@pytest.mark.parametrize(
    ("dashed", "underscored"),
    [
        ("SMF-L2.9-W1.9-LS3.9-FD", "SMF_L2.9-W1.9-LS3.9-FD"),
        ("LED-SMD-L1.6-W1.6-FD", "LED-SMD_L1.6-W1.6-FD"),
        ("DO-27-BD5.3-L9.0-P14.0-D1.3-R-RD", "DO-27_BD5.3-L9.0-P14.0-D1.3-R-RD"),
    ],
)
def test_a_dashed_name_reads_like_its_underscored_form(dashed, underscored):
    """A head cut at its first dimension gives the family, tokens and rotation the underscore form gives."""
    assert extract_family(dashed) == extract_family(underscored)
    assert parse_package_name(dashed) == parse_package_name(underscored)


@pytest.mark.parametrize(
    ("name", "rotation", "source", "confidence"),
    [
        ("SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL", 90, "naming_rule", "high"),
        ("SOT-23-3_L2.9-W1.6-P1.90-LS2.8-BR", 180, "naming_rule", "high"),
        ("QFN-48_L7.0-W7.0-P0.5-TL", 0, "naming_rule", "high"),
        ("SOT-223-3_L6.5-W3.5-P2.30-LS7.0-BR", 180, "naming_rule", "high"),
        ("CAP-SMD_BD6.3-L6.6-W6.6-FD", 0, "naming_rule", "high"),
        ("CAP-SMD_BD6.3-L6.6-W6.6-LS7.2-R-RD", 180, "naming_rule", "high"),
        ("LED-SMD_L1.6-W0.8-R-RD", 0, "naming_rule", "medium"),
        ("LED0603-RD_GREEN", 0, "naming_rule", "medium"),
        ("led0603-rd", 0, "naming_rule", "medium"),
        ("SOIC-8_L4.9-W3.9-P1.27-LS6.0-TR", 270, "naming_rule", "high"),
        ("CAP-SMD_BD5.0-L5.3-W5.3-T", 270, "naming_rule", "high"),
        ("CAP-SMD_BD5.0-L5.3-W5.3-B", 90, "naming_rule", "high"),
        ("SOD-882_L1.0-W0.6-RD", 0, "naming_rule", "medium"),
        ("LED0603-RD", 0, "naming_rule", "medium"),
        ("LED0603-FD", 180, "naming_rule", "medium"),
        ("SMA_L4.3-W2.6-LS5.2-RD", 0, "naming_rule", "medium"),
        ("VSON-10_L3.0-W3.0-TL-EP_TPS61230DRCR", 0, "naming_rule", "high"),
        ("C0603", 0, "family_default", "low"),
        ("R0402", 0, "family_default", "low"),
        ("USB-SMD_TYPECSMD8P-C9900161134", 0, "none", "none"),
        ("CONN-SMD_2P-P2.54-R-RD", 0, "none", "none"),
    ],
)
def test_parse_package_name(name, rotation, source, confidence):
    """Each package name maps to the rotation, source and confidence its tokens define."""
    result = parse_package_name(name)
    assert (
        result.rotation_correction,
        result.rotation_source,
        result.parser_confidence,
    ) == (
        rotation,
        source,
        confidence,
    )


def test_smf_is_treated_as_a_diode_family():
    """SMF (a TVS and Schottky package) reads its FD/RD tokens cathode-first, like other diodes."""
    assert parse_package_name("SMF_L2.8-W1.8-LS3.7-RD").rotation_correction == 0
    assert parse_package_name("SMF_L2.8-W1.8-LS3.7-FD-1").rotation_correction == 180
    assert "SMF" in CATHODE_PIN1_FAMILIES


def test_micro_melf_reads_cathode_first_without_a_hint():
    """MICRO-MELF is a MELF diode: its RD reads 0 like MELF and MINIMELF, hint or not."""
    result = parse_package_name("MICRO-MELF_L2.0-W1.2-RD")
    assert (result.rotation_correction, result.parser_confidence) == (0, "medium")
    assert parse_package_name("MICRO-MELF_L2.0-W1.2-FD").rotation_correction == 180


def test_lcsc_category_fallback_inverts_polarity_for_unlisted_diode_families():
    """A family the table does not know still inverts FD/RD when the caller knows the part is a diode.

    Any truthy hint counts, here the name of LCSC's "Diodes" category.
    """
    assert parse_package_name("SOD-110_L2.0-W1.2-FD").rotation_correction == 0
    assert (
        parse_package_name("SOD-110_L2.0-W1.2-FD", "Diodes").rotation_correction == 180
    )


def test_orientation_tokens_skip_dimensions_and_mpn_suffixes():
    """Dimensional tokens are not orientation, and MPN families yield nothing."""
    assert extract_orientation_tokens("SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL") == ["BL"]
    assert extract_orientation_tokens("CAP-SMD_BD6.3-L6.6-W6.6-LS7.2-R-RD") == [
        "R",
        "RD",
    ]
    assert extract_orientation_tokens("HDR-TH_2P-P2.54-V-R") == []
    assert extract_orientation_tokens("CASE-B_3528") == []
    assert extract_orientation_tokens("CASE-A_3216") == []
    assert extract_family("CASE-B_3528") == "CASE-B"


def test_a_corner_token_outranks_a_polarity_token():
    """A corner names where pin 1 is drawn, so an LED array's TR wins over its RD."""
    result = parse_package_name("LED-ARRAY-SMD_4P-L1.6-W1.5-TR-RD_TJ-S1615SW6TGLCRB-A5")
    assert (result.rotation_correction, result.parser_confidence) == (270, "high")
    assert result.notes == "corner TR→270° used; polarity RD→180° disagrees"
    assert (
        parse_package_name("LED-ARRAY-SMD_4P-L1.6-W1.5-TR-R-RD").rotation_correction
        == 270
    )


def test_a_polarity_token_outranks_an_edge_token():
    """On a two-terminal part the edge token is only as good as the library's pin 1, so RD wins over L."""
    result = parse_package_name("CAP-SMD_BD5.0-L5.3-W5.3-L-RD")
    assert result.rotation_correction == 180
    assert result.notes == "polarity RD→180° used; positional L→0° disagrees"


def test_a_corner_token_outranks_an_edge_token():
    """Without a polarity token, a corner still beats an edge token, wherever each sits."""
    result = parse_package_name("DIP-6_L8.5-W8.5-P2.54-LS10.8-BL-R")
    assert (result.rotation_correction, result.notes) == (90, None)


@pytest.mark.parametrize(
    "family_name",
    ["LED-ARRAY_6P-RD", "LED-ARRAY-SMD_2P-L3.5-W2.8-RD", "LED-ARRAY-TH_2P-RD"],
)
def test_led_arrays_ignore_the_cathode_hint(family_name):
    """LED-ARRAY and its SMD and TH variants keep the standard table even when the caller says diode."""
    result = parse_package_name(family_name, True)
    assert (result.rotation_correction, result.parser_confidence) == (180, "high")


@pytest.mark.parametrize(
    "name",
    ["MICROFC-30035-SMT-TR", "QTH-030-01-L-D-A"],
)
def test_a_bare_part_number_carries_no_tokens(name):
    """A name with no underscore, no family-and-digits code and no dimension is a part number."""
    assert extract_orientation_tokens(name) == []
    assert parse_package_name(name).rotation_source == "none"


def test_a_rule_shaped_name_without_an_underscore_still_carries_tokens():
    """A family code with digits, or dimension segments, marks a name that follows the rule."""
    assert extract_orientation_tokens("LED0603-RD") == ["RD"]
    assert extract_orientation_tokens("SOP-5-L4.4-W3.6-P1.27-LS7.0-TL") == ["TL"]


def test_only_the_connector_micro_families_are_part_numbers():
    """MICRO-USB and the card sockets are connectors; MICRO-8 is an IC package that reads its corner."""
    assert extract_orientation_tokens("MICRO-USB-SMD_5P-P0.65-H-F-B_C10418") == []
    assert extract_orientation_tokens("MICRO-SD-SMD_TF-PUSH-L") == []
    assert (
        parse_package_name("MICRO-8_L3.0-W3.0-P0.65-LS4.9-BL").rotation_correction == 90
    )


def test_chip_diodes_read_cathode_first():
    """The D family (D0603-RD, D0402-BI) numbers the cathode 1 like the other diode families."""
    assert "D" in CATHODE_PIN1_FAMILIES
    rd = parse_package_name("D0603-RD")
    assert (rd.rotation_correction, rd.parser_confidence) == (0, "medium")
    assert parse_package_name("D0603-FD").rotation_correction == 180
    assert parse_package_name("D0402-BI").rotation_correction == 0
    assert parse_package_name("D0402").rotation_source == "family_default"


def test_a_chip_code_keeps_its_family_behind_a_suffix():
    """A suffix after a chip code changes neither its family default nor an LED's cathode-first reading."""
    plain = parse_package_name("R0603")
    suffixed = parse_package_name("R0603_1")
    assert (suffixed.rotation_source, suffixed.rotation_correction) == (
        plain.rotation_source,
        plain.rotation_correction,
    )
    assert parse_package_name(
        "LED0603_L1.6-W0.8-RD_CSL0901S10T1"
    ).rotation_correction == (parse_package_name("LED0603-RD").rotation_correction)
