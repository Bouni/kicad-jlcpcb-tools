"""Tests for the package designator the part selector prefills into its search.

The part selector ANDs this token with the board value, so a token the catalog
never writes does not merely widen the result -- it empties it.  These cases
are anchored on spellings checked against a 717,025-part catalog snapshot.
"""

import pytest

from dataview_highlight import simplify_footprint_name


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        # The qualifier the old rule returned is named in each comment; every
        # one of them matches zero catalog rows, which emptied the search.
        pytest.param("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOIC-8", id="was-P1.27mm"),
        pytest.param("Package_QFP:LQFP-48_7x7mm_P0.5mm", "LQFP-48", id="was-P0.5mm"),
        pytest.param(
            "Package_TO_SOT_SMD:SOT-23-5_HandSoldering",
            "SOT-23-5",
            id="was-HandSoldering",
        ),
        pytest.param(
            "Resistor_SMD:R_0805_2012Metric_Pad1.20x1.40mm_HandSolder",
            "0805",
            id="chip-size-survives-a-hand-solder-variant",
        ),
        pytest.param(
            "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm",
            "QFN-24",
            id="was-EP2.7x2.7mm",
        ),
    ],
)
def test_qualifier_segments_are_not_mistaken_for_the_package(footprint, expected):
    """The trailing segment of a KiCad name qualifies the package, it is not it."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        pytest.param("Resistor_SMD:R_0603_1608Metric", "0603", id="imperial-code"),
        pytest.param("Capacitor_SMD:C_01005_0402Metric", "01005", id="five-digit"),
        pytest.param("LED_SMD:LED_0805_2012Metric", "0805", id="class-prefix-ignored"),
    ],
)
def test_chip_sizes_use_the_imperial_code(footprint, expected):
    """The catalog spells chip packages with the imperial code, never the metric."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        # KiCad 4 libraries wrote the code without its metric companion, and a
        # board built from them keeps those footprint ids after every upgrade.
        pytest.param("Resistors_SMD:R_0603", "0603", id="kicad4-resistor"),
        pytest.param(
            "Capacitors_SMD:C_0603_HandSoldering",
            "0603",
            id="kicad4-hand-solder-variant",
        ),
        pytest.param("LEDs:LED_0805", "0805", id="kicad4-led"),
        pytest.param("Fuse:Fuse_1206", "1206", id="four-letter-class"),
        pytest.param("Resistor_SMD:R_01005", "01005", id="five-digit"),
    ],
)
def test_bare_chip_sizes_are_read_after_a_class_prefix(footprint, expected):
    """The code alone is still the code when a class letter introduces it."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "reason"),
    [
        pytest.param(
            "Battery:BatteryHolder_Keystone_1060_1x2032",
            "1060 is a Keystone part number, and BatteryHolder is a word",
            id="part-number",
        ),
        pytest.param(
            "Crystal:Crystal_SMD_0603-2Pin_6.0x3.5mm",
            "the size is not a whole segment, and Crystal is a word",
            id="crystal-size",
        ),
        pytest.param(
            "Connector_Audio:Jack_3.5mm_Lumberg_1503_02_Horizontal",
            "1503 is a Lumberg series, not the second segment",
            id="series-number",
        ),
        pytest.param(
            "util-Dipole1090:Dipole_1090_Arms",
            "1090 is a frequency, and Dipole is a word",
            id="long-class-word",
        ),
        pytest.param(
            "Connector_Audio:Jack_1503_02",
            "Jack is short enough to be mistaken for a class, and is not one",
            id="short-word-not-a-class",
        ),
    ],
)
def test_four_digits_that_are_not_a_chip_code_stay_unread(footprint, reason):
    """Digits qualify as a chip code only as a whole segment after class letters."""
    assert simplify_footprint_name(footprint) == "", reason


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        pytest.param("Package_TO_SOT_SMD:SOT-23", "SOT-23", id="first-segment"),
        pytest.param("Diode_SMD:D_SOD-123", "SOD-123", id="after-class-prefix"),
        pytest.param(
            "Package_SO:JEITA_SOIC-16_3.9x9.9mm_P1.27mm",
            "SOIC-16",
            id="after-standards-prefix",
        ),
        pytest.param(
            "Package_DFN_QFN:AMS_QFN-4-1EP_2x2mm_P0.95mm_EP0.7x1.6mm",
            "QFN-4",
            id="after-manufacturer-prefix",
        ),
        pytest.param("Diode_SMD:D_SMA", "SMA", id="family-with-no-pin-count"),
        pytest.param("Package_DIP:DIP-8_W7.62mm_Socket", "DIP-8", id="through-hole"),
    ],
)
def test_the_package_family_is_found_wherever_it_sits(footprint, expected):
    """A manufacturer or standards body may come first; the family still wins."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        # The catalog uses none of KiCad's letter suffixes: it writes SOD-323,
        # whose rows include the SOD-323F and SOD-323FL ones, and has no
        # SOIC-8-1EP at all.
        pytest.param("Diode_SMD:D_SOD-323F", "SOD-323", id="catalog-drops-F"),
        pytest.param("Diode_SMD:D_SOD-123F", "SOD-123", id="catalog-drops-F-again"),
        pytest.param(
            "Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm",
            "SOIC-8",
            id="catalog-drops-exposed-pad",
        ),
        pytest.param(
            "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.7x2.7mm",
            "QFN-24",
            id="catalog-drops-exposed-pad-count",
        ),
    ],
)
def test_kicad_letter_suffixes_are_dropped(footprint, expected):
    """Letters are KiCad's; the catalog names the family and its numbers."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        # The pin count is part of the designator and worth keeping:
        # SOT-223-5 reaches 58 rows where SOT-223 reaches 2,463.
        pytest.param("Package_TO_SOT_SMD:SOT-23-6", "SOT-23-6", id="sot-23-6"),
        pytest.param("Package_TO_SOT_SMD:SOT-223-5", "SOT-223-5", id="sot-223-5"),
        pytest.param("Package_TO_SOT_THT:TO-220-3_Vertical", "TO-220-3", id="to-220-3"),
        pytest.param("Package_TO_SOT_SMD:SOT-89-3", "SOT-89-3", id="sot-89-3"),
    ],
)
def test_pin_counts_are_kept(footprint, expected):
    """Trimming the pin count throws away precision the catalog does offer."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        # KiCad puts a lettered subtype between family and pin count. The
        # catalog has no such spelling -- DFN-S-8 matches nothing, DFN-8
        # matches 5,009 parts -- so the subtype is recognised and dropped.
        pytest.param(
            "Package_DFN_QFN:DFN-S-8-1EP_6x5mm_P1.27mm", "DFN-8", id="dfn-subtype"
        ),
        pytest.param(
            "Package_DFN_QFN:LFCSP-VQ-24-1EP_4x4mm_P0.5mm_EP2.642x2.642mm",
            "LFCSP-24",
            id="lfcsp-subtype",
        ),
        # A trailing -U is not a subtype, and nothing useful is left once it is
        # refused.  The catalog spells this HC-49S, HC-49S-SMD and HC-49U, with
        # a dash KiCad omits: HC49 matches 0 rows where HC-49 matches 2,951.
        # Returning HC49 would empty the search, and the dash cannot be
        # inferred -- DFN1006 is undashed at 2,067 rows, so the catalog has no
        # rule here, only per-family spellings.  Saying nothing is correct.
        pytest.param("Crystal:Crystal_HC49-U_Vertical", "", id="undashed-family"),
    ],
)
def test_lettered_subtypes_are_dropped(footprint, expected):
    """The subtype is KiCad's; the catalog names the family and the pin count."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        pytest.param("Diode_SMD:D_SMA_Handsoldering", "SMA", id="hand-solder"),
        pytest.param("Diode_SMD:D_SMB_Modified", "SMB", id="modified"),
        pytest.param("Diode_SMD:D_MELF_Handsoldering", "MELF", id="melf"),
        # The JEDEC size code follows the family for MELF resistors.
        pytest.param("Resistor_SMD:R_MELF_MMB-0207", "MELF", id="melf-jedec-code"),
        pytest.param(
            "Resistor_SMD:R_MiniMELF_MMA-0204", "MiniMELF", id="minimelf-jedec-code"
        ),
    ],
)
def test_one_trailing_segment_does_not_hide_a_family_with_no_size(footprint, expected):
    """SMA, SMB and MELF carry no pin count, so only the tail distinguishes them."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "expected"),
    [
        # The catalog writes SMD,D6.3xL7.7mm; boards fit whatever height is in
        # stock, so only the diameter is worth constraining.
        pytest.param("Capacitor_SMD:CP_Elec_6.3x5.9", "SMD,D6.3x", id="diameter-only"),
        pytest.param("Capacitor_SMD:CP_Elec_4x5.8", "SMD,D4x", id="integer-diameter"),
        pytest.param(
            "Capacitor_SMD:CP_Elec_6.3x5.4_Nichicon",
            "SMD,D6.3x",
            id="manufacturer-suffix-ignored",
        ),
        # CASE-B-3528-21(mm) is the catalog's spelling of this tantalum case.
        # Only the land size is kept, for the same reason as the cans above:
        # 3528-15 reaches nothing and 7343-40 reaches two rows, where 3528-
        # reaches 811 and 7343- reaches 692.  The dash stays because the bare
        # size is also a chip package -- 1608- is 377 rows, 1608 is 3,724.
        pytest.param(
            "Capacitor_Tantalum_SMD:CP_EIA-3528-21_Kemet-B",
            "3528-",
            id="tantalum-land-size",
        ),
        pytest.param(
            "Capacitor_Tantalum_SMD:CP_EIA-3528-15_AVX-H",
            "3528-",
            id="tantalum-height-ignored",
        ),
    ],
)
def test_electrolytics_match_on_the_dimension_the_catalog_shares(footprint, expected):
    """KiCad measures these cans differently from the catalog; share what overlaps."""
    assert simplify_footprint_name(footprint) == expected


@pytest.mark.parametrize(
    ("footprint", "reason"),
    [
        pytest.param(
            "Button_Switch_THT:SW_DIP_SPSTx01_Slide_9.78x4.72mm_W8.61mm_P2.54mm",
            "a DIP switch is listed under Plugin,P=2.54mm, not DIP",
            id="dip-switch",
        ),
        pytest.param(
            "Connector_Card:microSD_HC_Hirose_DM3AT-SF-PEJM5",
            "the HC in microSD is a capacity class, not an HC can",
            id="microsd",
        ),
        pytest.param(
            "Package_TO_SOT_SMD:DirectFET_SC",
            "two characters reach Library.search as a LIKE over the description",
            id="too-short",
        ),
        pytest.param(
            "RF_Connector_SMA:SMA_Amphenol_132134-10_Vertical",
            "an SMA connector is not an SMA diode package",
            id="rf-connector",
        ),
        pytest.param(
            "Connector_Coaxial:SMB_Jack_Vertical",
            "two trailing segments means the family word describes something else",
            id="smb-jack",
        ),
        # Letters only count as a subtype when a dashed number follows, or the
        # digits beside them read as a package that was never there: MELF-RM10
        # would become MELF10, which matches nothing at all.
        pytest.param(
            "Diode_SMD:D_MELF-RM10_Universal_Handsoldering",
            "RM10 is a size code, so MELF-RM10 is not a MELF10",
            id="melf-rm10",
        ),
        pytest.param(
            "Package_DFN_QFN:Texas_VQFN-RNR0011A-11",
            "RNR0011A is a drawing number, so this is not a VQFN0011",
            id="vqfn-drawing-number",
        ),
    ],
)
def test_family_words_used_as_adjectives_are_rejected(footprint, reason):
    """A family name inside a longer name is describing the part, not packaging it."""
    assert simplify_footprint_name(footprint) == "", reason


@pytest.mark.parametrize(
    ("footprint", "reason"),
    [
        pytest.param(
            "Symbol:Symbol_CC-Attribution_CopperTop_Small",
            "Small begins with SMA",
            id="small-is-not-an-sma",
        ),
        pytest.param(
            "Inductor_THT:L_Axial_L9.5mm_D4.0mm_P2.54mm_Vertical_Fastron_SMCC",
            "Fastron's SMCC series begins with SMC",
            id="smcc-is-not-an-smc",
        ),
    ],
)
def test_a_segment_that_merely_starts_with_a_family_is_not_a_package(footprint, reason):
    """Letters may only follow a number, or every word beginning SMA is a package."""
    assert simplify_footprint_name(footprint) == "", reason


@pytest.mark.parametrize(
    "footprint",
    [
        pytest.param("Connector_JST:JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical", id="jst"),
        pytest.param(
            "TestPoint:TestPoint_Keystone_5000-5004_Miniature", id="testpoint"
        ),
        pytest.param("MountingHole:MountingHole_3.2mm_M3", id="mounting-hole"),
        pytest.param(
            "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P7.62mm_Horizontal",
            id="axial-resistor",
        ),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
    ],
)
def test_footprints_with_no_catalog_package_yield_nothing(footprint):
    """Saying nothing leaves the value searching alone, which beats a dead token."""
    assert simplify_footprint_name(footprint) == ""
