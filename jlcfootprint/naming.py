"""EasyEDA Footprint Naming Rule suffix parser: the pin-1 position a package name encodes.

Implements the rotation-correction lookup defined by the EasyEDA Footprint Naming
Rule Reference (MIT licensed, jointly authored by LCSC and EasyEDA engineering).

The naming rule encodes the footprint's pin-1 orientation in the package name:
  {family}[{dimensions}]-{orientation-token*}

  Family examples: C, R, LED, SOIC-8, CAP-SMD, QFN-48
  Dimension tokens (filtered, not orientation): L{n}, W{n}, LS{n}, P{n}, BD{n}, EP
  Orientation tokens:
    TL / TR / BL / BR  -- pin-1 at a corner of the IC outline
    L / R / T / B      -- pin-1 at an edge (2-pin or axial packages)
    FD / RD / BI       -- polarity direction for 2-terminal polarized parts

Rotation conventions:
  - Angles are degrees, counter-clockwise as KiCad counts rotation.  Each one is
    the turn that carries KiCad's footprint onto JLC's drawing: KiCad's pin 1
    (top-left, or left for two pins) lands where the token puts JLC's pin 1.
  - The CPL counts the opposite turn, JLC's drawing back onto KiCad's footprint,
    so the CPL correction is the negative (``geometry.naming_rule_to_cpl``): a
    SOIC-8 with a -BL token is 90° here and 270° in the CPL.
  - Standard JLCPCB reference: pin-1 at top-left → 0° correction.
  - For 2-pin polarized parts: pin-1 (positive terminal) on left → 0° correction.

Cathode-is-pin-1 exception (LEDs and standard diodes):
  In KiCad, both LED footprints and standard diode footprints (Diode_SMD, Diode_TH)
  use pin-1 = cathode (K), not anode.  EasyEDA's FD (anode on left) therefore places
  K on the right → 180° correction.  RD (anode on right) places K on the left → 0°.
  This inverts the FD/RD mapping relative to capacitors.
  Affected families include LED, LED-SMD, LED-TH, the chip diodes (D), the DO axial
  packages, SMA/SMB/SMC, SOD-123/323/523/923 and MELF/MINIMELF/MICROMELF/MICRO-MELF;
  ``CATHODE_PIN1_FAMILIES`` is the full list.

Which token wins: a corner token (TL/TR/BL/BR), then a polarity token (FD/RD/BI),
then an edge token (L/R/T/B); ``parse_package_name`` gives the reasons.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional

# ---------------------------------------------------------------------------
# Rotation correction lookup tables
# ---------------------------------------------------------------------------

# Pin-1 corner/edge position → correction (every family, cathode-first ones included)
_PIN1_CORRECTION: dict[str, int] = {
    "TL": 0,  # top-left  → already standard → 0°
    "TR": 270,  # KiCad's top-left pin 1 lands on JLC's top-right after 270° CCW
    "BL": 90,  # KiCad's top-left pin 1 lands on JLC's bottom-left after 90° CCW
    "BR": 180,  # KiCad's top-left pin 1 lands on JLC's bottom-right after 180°
    "L": 0,  # left → standard 2-pin horizontal → 0°
    "R": 180,  # right → flipped → 180°
    "T": 270,  # KiCad's left pin 1 lands at the top after 270° CCW
    "B": 90,  # KiCad's left pin 1 lands at the bottom after 90° CCW
}

# Polarity direction → correction (standard: positive terminal = KiCad pin-1)
_POLARITY_CORRECTION_STANDARD: dict[str, int] = {
    "FD": 0,  # forward direction: positive on left → KiCad standard → 0°
    "RD": 180,  # reverse direction: positive on right → 180°
    "BI": 0,  # bidirectional → no preferred orientation → 0°
}

# Polarity direction → correction for LED family:
# KiCad LED footprint has pin-1 = cathode (K), not anode.
# EasyEDA FD (anode on left) → K on right → 180° correction.
# EasyEDA RD (anode on right) → K on left → 0° correction.
_POLARITY_CORRECTION_LED: dict[str, int] = {
    "FD": 180,
    "RD": 0,
    "BI": 0,
}

# All recognised orientation tokens
_ALL_ORIENTATION_TOKENS = set(_PIN1_CORRECTION) | set(_POLARITY_CORRECTION_STANDARD)

# The positional tokens that name a corner of the outline.  A corner names where pin 1
# is drawn, so it outranks a polarity token; an edge token (L/R/T/B) on a two-terminal
# part is only as good as the library's choice of which terminal is pin 1, so a
# polarity token outranks it.
_CORNER_TOKENS = frozenset({"TL", "TR", "BL", "BR"})

# ---------------------------------------------------------------------------
# MPN-suffix family denylist
# ---------------------------------------------------------------------------
#
# These families always use a free-form manufacturer part number (MPN) as their
# package suffix (the `_[SN/MPN]` format in the EasyEDA naming rule reference).
# Any token that looks like an orientation token inside an MPN is a product
# code, not an EasyEDA geometric orientation indicator.
#
# Matching is done on the first hyphen-separated word of the extracted family,
# so "CONN-SMD", "CONN-TH", and "CONN" all map to root "CONN", or on its first two
# words for a root that names one (MICRO-USB, not every MICRO-something).
#
# Source: EasyEDA Footprint Naming Rule Reference §§3.2, 4.2, 4.3, 5.x
#
# Excluded from this list (they DO carry valid orientation tokens):
#   - LED, LED-SMD, LED-TH, LED-SEG   — have TL/TR/BL/BR or FD/RD tokens
#   - BUZ, PSK, MIC                    — structured format with L/R-(FD/RD)
#   - OSC                              — square packages use TL/TR/BL/BR
#   - all standard passives (R,C,L,D)  — handled by family_default path
#
# Note: older or third-party EasyEDA footprints occasionally deviate from the
# naming rule, so this filter may suppress a small number of valid tokens.
_MPN_SUFFIX_FAMILY_ROOTS: frozenset[str] = frozenset(
    {
        # §4.1 Standard Header — V/H/R/C/S params, no orientation tokens in format
        "HDR",
        # §4.2 Non-Standard Header / Connector — _[SN/MPN] format
        "CONN",
        "IDC",
        # §4.3 Special Function Connector — _[SN/MPN] for all non-regular shapes
        # Note: MICRO-USB-SMD → root "MICRO-USB"; USB-C-SMD → root "USB".  Both are
        # connectors.  MICRO alone would also take IC and diode packages (MICRO-8,
        # MICRO-MELF), so only the connector and card-socket MICRO families are listed.
        "FPC",
        "MICRO-USB",
        "MICRO-B",
        "MICRO-SD",
        "MICRO-SIM",
        "USB",
        "AUDIO",
        "DP",
        "HDMI",
        "DVI",
        "DSUB",
        "VGA",
        "SD",
        "TF",
        "SIM",
        "RF",
        "DIMM",
        "ATX",
        "PCI",
        "SATA",
        "ATA",
        "RJ11",
        "RJ12",
        "RJ22",
        "RJ45",
        "RJ48",
        "FIBER",
        "LPC",
        # Note: root matching uses first hyphen-segment (e.g. RJ45-TH → root RJ45),
        # so "RJ45" must be listed rather than just "RJ".
        # §3.2 Non-standard semiconductor packages — _[SN/MPN] format
        "TRS",
        "IC",
        "MCU",
        "DRAM",
        "SEMI",
        # §5 Other discrete devices — _[SN/MPN] format
        "RELAY",
        "SENSOR",
        "OPTO",
        "XFMR",
        "BAT",
        "ANT",
        # §5.8 Switch/Key — EH (extra hole) token but no orientation token
        "SW",
        "KEY",
    }
)

# Families where KiCad footprints use pin-1 = cathode (K), not anode.
# For these families FD/RD polarity is inverted relative to the standard table:
#   RD (anode on right → cathode on left) → 0° correction
#   FD (anode on left  → cathode on right) → 180° correction
#
# LED families: KiCad's LED footprints use pin-1 = K.
#   LED-ARRAY is deliberately excluded: multi-LED packages often use pin-1 = anode.
#
# Standard diode packages: KiCad Diode_TH and Diode_SMD footprints also use
#   pin-1 = K (cathode).  Aligning the pads of DO-15, SMA and SOD-323 parts agrees:
#   an RD package's cathode, KiCad's pad 1, is on the left, at 0°.  The chip-diode
#   family D agrees as well: over EasyEDA's library, all nine of its horizontal
#   two-pad drawings with an FD/RD token put pad 1 on the token's cathode side.
_CATHODE_PIN1_FAMILIES = frozenset(
    {
        # LED families
        "LED",
        "LED-SMD",
        "LED-TH",
        # Through-hole axial diodes (DO-2xx series)
        "DO-14",
        "DO-15",
        "DO-27",
        "DO-34",
        "DO-35",
        "DO-41",
        # DO-201 series (large-body high-current rectifiers, same TH axial convention)
        "DO-201",
        "DO-201AD",
        "DO-201AE",
        # Generic EasyEDA diode family names
        "D",  # chip diodes named by size (D0603-RD, D0402-BI)
        "DIO-TH",  # through-hole generic
        "DIO-SMD",  # SMD generic (same cathode-is-pin-1 convention as other SMD diodes)
        # SMD diodes — DO-214 variants with standardised numeric names
        # DO-214AA = SMA, DO-214AB = SMB, DO-214AC = SMC (some EasyEDA footprints use the DO name)
        "DO-214AA",
        "DO-214AB",
        "DO-214AC",
        # SMD diodes — DO-214 named variants (SMA=AA, SMB=AB, SMC=AC) and flat variants
        "SMA",
        "SMB",
        "SMC",
        "SMAF",
        "SMBF",
        "SMCF",  # flat/low-profile DO-219 variants
        "SMAG",
        "SMBG",
        "SMCG",  # guard-ring variants (same footprint/convention as SMA/B/C)
        # Large SMD power diode (Microsemi/Vishay)
        "DO-218AB",
        # DO-219AB / SMF: TVS and Schottky diode packages.  Nothing in their names
        # says diode, so they are listed rather than left to a caller's hint.
        "SMF",
        "DO-219AB",
        # SOD-882 / SOD-882D: 2-lead DFN diodes (KiCad's D_SOD-882 has pin 1 = K), so
        # an RD token reads 0°.
        "SOD-882",
        "SOD-882D",
        # Vishay PowerDI series (SOD-123 footprint-compatible)
        "POWERDI-123",
        "POWERDI-323",
        # 2-terminal diode DFN/TSSLP variants (polarity token implies diode use)
        "DFN1610-2L",  # 1.6×1.0mm 2-lead DFN — almost exclusively diodes at this size
        "TSSLP-2-3",  # Thin Stacked SLP 2- or 3-pad — diode/TVS packages
        # Small SMD diodes — SOD (Small Outline Diode) series
        "SOD-123",
        "SOD-123F",
        "SOD-123FL",
        "SOD-123W",
        "SOD-323",
        "SOD-323F",
        "SOD-523",
        "SOD-523F",
        "SOD-923",
        # MELF diodes (MELF for resistors carries no polarity token, so safe to include)
        "MELF",
        "MINIMELF",
        "MICROMELF",
        "MICRO-MELF",  # EasyEDA's spelling (MICRO-MELF_L2.0-W1.2-RD)
    }
)

# The public name for the families whose FD/RD tokens are read cathode-first.
# Polarized capacitors never belong here: listing a family inverts its FD/RD rotation.
CATHODE_PIN1_FAMILIES: frozenset[str] = _CATHODE_PIN1_FAMILIES

# Families that must never be treated as cathode-is-pin-1 even when a caller says the part
# is a diode: multi-LED arrays usually put the anode on pin 1.  A listed family covers
# its hyphenated variants (LED-ARRAY-SMD, LED-ARRAY-TH).
_NEVER_CATHODE_FAMILIES: frozenset[str] = frozenset({"LED-ARRAY"})


def _never_cathode(family: str) -> bool:
    """Return True when the family, or the family it is a variant of, is never cathode-first."""
    return any(
        family == root or family.startswith(root + "-")
        for root in _NEVER_CATHODE_FAMILIES
    )


# ---------------------------------------------------------------------------
# Dimensional token filter
# ---------------------------------------------------------------------------

# A segment is dimensional if it matches one of these patterns; the first dimensional
# segment of a name ends its family (SMF-L2.9-W1.9-LS3.9-FD → SMF).
# Note: bare "L" and "T" are orientation tokens — only "L{digit}" is dimensional.
_DIMENSIONAL_RE = re.compile(
    r"^(?:"
    r"L\d"  # body length: L4.9, L1.6, ...
    r"|W\d"  # body width
    r"|LS\d"  # lead span
    r"|P\d"  # pin pitch
    r"|BD\d"  # body diameter
    r"|EP"  # exposed pad (no number)
    r")"
)


def _is_dimensional(token: str) -> bool:
    """Return True for dimensional segments such as L4.9, LS6.0 or EP."""
    return bool(_DIMENSIONAL_RE.match(token))


def _before_dimensions(head: str) -> str:
    """Return ``head`` up to its first dimensional segment: ``SMF-L2.9-W1.9-FD`` → ``SMF``.

    The first segment always stays, so a family code such as ``L0603`` is never cut.
    """
    parts = head.split("-")
    for index in range(1, len(parts)):
        if _is_dimensional(parts[index]):
            return "-".join(parts[:index])
    return head


# ---------------------------------------------------------------------------
# Family extraction
# ---------------------------------------------------------------------------

# A name with an underscore has its family before the first underscore
# ("CAP-SMD_BD6.3-..." → "CAP-SMD"), unless that head carries its own tokens
# (LED0603-RD_GREEN).  Then, as for a name without an underscore, the family is the
# leading run of letters, hyphen-joined letter groups included, that ends at a digit
# or at the end: "LED0603-RD" → "LED", "C0603" → "C".  Either way the head is first
# cut at its first dimensional segment, so "SMF-L2.9-W1.9-LS3.9-FD" and
# "SMF_L2.9-W1.9-LS3.9-FD" read the same family.
_LEADING_ALPHA_RE = re.compile(r"^([A-Z]+(?:-[A-Z]+)*)(?=\d|$)")
# A head that carries its own orientation token looks like LED0603-RD: letters, digits, token.
_HEAD_WITH_DIGITS_RE = re.compile(r"^[A-Z]+(?:-[A-Z]+)*\d")
# A chip code, a standard passive's or an LED's family letters followed by a
# four-digit size, reads as that family before an underscore too: "R0603_1",
# "L0402-SMD_L1.0-..." and "LED0603_L1.6-W0.8-RD_..." are R, L and LED, as "R0603"
# is R.  Other heads before an underscore keep their digits: over EasyEDA's library
# the leading-letter rule would make D2PAK-... a D, a diode family, and RJ45-TH an RJ,
# which the manufacturer-part-number denylist does not hold.
_CHIP_CODE_RE = re.compile(r"^(R|C|L|D|F|LED)\d{4}(?=-|$)")


def extract_family(package_name: str) -> str:
    """Return the family prefix of a package name.

    Examples:
        "C0603"                          → "C"
        "L0402-SMD_L1.0-W0.5-MLK1005"    → "L"
        "LED0603-RD"                     → "LED"
        "SOIC-8_L4.9-W3.9-P1.27-BL"    → "SOIC-8"
        "CAP-SMD_BD6.3-L6.6-W6.6-FD"   → "CAP-SMD"
        "QFN-48_L7.0-W7.0-P0.5-BL"     → "QFN-48"
        "SMF-L2.9-W1.9-LS3.9-FD"       → "SMF"

    """
    package_name = package_name.upper()
    head = package_name.split("_", 1)[0]
    if "_" in package_name and not _head_tokens(head):
        head = _before_dimensions(head)
        chip = _CHIP_CODE_RE.match(head)
        return chip.group(1) if chip else head
    head = _before_dimensions(head)
    m = _LEADING_ALPHA_RE.match(head)
    if m:
        return m.group(1).rstrip("-")
    return head  # fallback: return as-is


def _head_tokens(head: str) -> list[str]:
    """Return orientation tokens carried by a pre-underscore head such as ``LED0603-RD``.

    Only a head shaped family-plus-digits qualifies; a bare code such as ``CASE-B``
    (a tantalum case size) has no digits and its ``B`` is not an orientation.
    """
    if not _HEAD_WITH_DIGITS_RE.match(head):
        return []
    return _tokens_in(head)


def _tokens_in(section: str) -> list[str]:
    """Return the orientation tokens found in one hyphen-separated section, in order."""
    return [
        tok.upper()
        for tok in section.split("-")
        if tok.upper() in _ALL_ORIENTATION_TOKENS
    ]


def _is_mpn_suffix_family(family: str) -> bool:
    """Return True for a family whose suffix is a free manufacturer part number."""
    words = family.split("-")
    return (
        words[0] in _MPN_SUFFIX_FAMILY_ROOTS
        or "-".join(words[:2]) in _MPN_SUFFIX_FAMILY_ROOTS
    )


# ---------------------------------------------------------------------------
# Orientation token extraction
# ---------------------------------------------------------------------------


def extract_orientation_tokens(package_name: str) -> list[str]:
    """Return all orientation tokens found in the package name suffix.

    Dimensional tokens (L4.9, W3.9, LS6.0, etc.) are filtered out.
    Returns tokens in the order they appear (last one wins if lookup differs).

    For families in _MPN_SUFFIX_FAMILY_ROOTS (connectors, relays, etc.) the
    entire suffix is treated as a free-form manufacturer part number and no
    orientation tokens are extracted, avoiding false positives from product
    codes that happen to look like EasyEDA orientation tokens.  A name with no
    underscore is read only when it is shaped like the rule's names (a family code
    with digits, LED0603-RD, or dimensional segments, SOP-5-L4.4-W3.6-P1.27-TL);
    anything else is a bare manufacturer part number, whose -TR (tape and reel)
    or -L is no orientation.
    """
    package_name = package_name.upper()
    family = extract_family(package_name)
    if _is_mpn_suffix_family(family):
        return []

    # The naming rule uses a second underscore to separate the geometric suffix from a
    # trailing MPN (VSON-10_L3.0-W3.0-TL-EP_TPS61230DRCR); only the segment between the
    # first and second underscore carries valid orientation tokens.  Some names put the
    # tokens before the first underscore instead and a colour or MPN after it
    # (LED0603-RD_GREEN); when the middle segment yields nothing, the head is scanned.
    head, _, rest = package_name.partition("_")
    if rest:
        tokens = _tokens_in(rest.split("_", 1)[0])
        if tokens:
            return tokens
        return _head_tokens(head)
    if _HEAD_WITH_DIGITS_RE.match(head) or _before_dimensions(head) != head:
        return _tokens_in(head)
    return []


# ---------------------------------------------------------------------------
# Parse result
# ---------------------------------------------------------------------------


class SuffixParseResult(NamedTuple):
    """Rotation derived from a package name, with provenance."""

    rotation_correction: int  # 0, 90, 180, or 270
    rotation_source: str  # 'naming_rule' | 'family_default' | 'none'
    parser_confidence: str  # 'high' | 'medium' | 'low' | 'none'
    family: str
    parsed_suffix: Optional[str]  # the orientation token(s) found, comma-joined
    notes: Optional[str]


def parse_package_name(
    package_name: str,
    cathode_is_pin1: Optional[bool] = None,
) -> SuffixParseResult:
    """Parse a package name and return the rotation correction.

    This is the main entry point for the suffix parser.

    Args:
        package_name: The EasyEDA package name string.
        cathode_is_pin1: A caller's knowledge that the part is a diode or LED whose
            KiCad footprint has pin 1 = cathode (for example from the part's catalog
            category, or from the schematic's pin functions).  Used only when the family
            is not already listed, and never for LED-ARRAY families
            (_NEVER_CATHODE_FAMILIES, variants included).

    Lookup order:
    1. A corner token (TL/TR/BL/BR) when one is present: a corner names where pin 1
       is drawn, so it wins even over a polarity token.  Over EasyEDA's library, 81
       of the 85 multi-pin drawings whose names carry both put pad 1 at the named
       corner.
    2. A polarity token (FD/RD/BI) otherwise: it names a physical direction, so it
       beats an edge token; cathode-is-pin-1 families (LEDs, standard diodes) read
       it from the inverted table, at medium confidence.
    3. An edge token (L/R/T/B) otherwise.  On a two-terminal part it is only as
       good as the library's choice of which terminal is pin 1, which is why a
       polarity token outranks it.
    4. No orientation token in a standard passive family (R, C, L, D, F): 0° with
       'family_default' source and low confidence.
    5. No orientation token otherwise, MPN-suffix families included: 0° with
       'none' source and no confidence.

    When a polarity token disagrees with the corner or the edge token it is
    ranked against, a note records the other token's value; an edge token
    outranked by a corner gets no note.  The rotation is in the table's sense;
    the CPL correction is its negative (module docstring).

    """
    family = extract_family(package_name)
    tokens = extract_orientation_tokens(package_name)

    positional = [t for t in tokens if t in _PIN1_CORRECTION]
    corners = [t for t in positional if t in _CORNER_TOKENS]
    polarity = [t for t in tokens if t in _POLARITY_CORRECTION_STANDARD]

    if not tokens:
        # No orientation token at all.
        # Standard passive families (R, C, L, D, F) default to 0°; others unknown.
        is_standard_passive = family.upper() in {"R", "C", "L", "D", "F"}
        if is_standard_passive:
            return SuffixParseResult(
                rotation_correction=0,
                rotation_source="family_default",
                parser_confidence="low",
                family=family,
                parsed_suffix=None,
                notes="no orientation token; standard passive assumed 0°",
            )
        return SuffixParseResult(
            rotation_correction=0,
            rotation_source="none",
            parser_confidence="none",
            family=family,
            parsed_suffix=None,
            notes=None,
        )

    family_upper = family.upper()
    cathode_via_family = family_upper in _CATHODE_PIN1_FAMILIES
    cathode_via_hint = (
        not cathode_via_family
        and bool(cathode_is_pin1)
        and not _never_cathode(family_upper)
    )
    is_cathode_family = cathode_via_family or cathode_via_hint
    pol_table = (
        _POLARITY_CORRECTION_LED if is_cathode_family else _POLARITY_CORRECTION_STANDARD
    )

    rotation: Optional[int] = None
    used_token: Optional[str] = None
    confidence = "high"
    notes = None

    if corners:
        used_token = corners[-1]
        rotation = _PIN1_CORRECTION[used_token]
        if polarity and pol_table[polarity[-1]] != rotation:
            notes = (
                f"corner {used_token}→{rotation}° used; polarity "
                f"{polarity[-1]}→{pol_table[polarity[-1]]}° disagrees"
            )
    elif polarity:
        used_token = polarity[-1]
        rotation = pol_table[used_token]
        if cathode_via_family:
            confidence = "medium"
            notes = "cathode-is-pin1 family: FD/RD polarity inverted relative to standard convention"
        elif cathode_via_hint:
            confidence = "medium"
            notes = "cathode-is-pin1 by caller's hint: FD/RD polarity inverted"
        if positional:
            pos_rotation = _PIN1_CORRECTION[positional[-1]]
            if pos_rotation != rotation:
                notes = (
                    f"polarity {used_token}→{rotation}° used; positional "
                    f"{positional[-1]}→{pos_rotation}° disagrees"
                )
    else:
        used_token = positional[-1]
        rotation = _PIN1_CORRECTION[used_token]

    all_tokens = positional + [t for t in polarity if t not in positional]
    parsed_suffix = ",".join(all_tokens) if all_tokens else None

    return SuffixParseResult(
        rotation_correction=rotation,
        rotation_source="naming_rule",
        parser_confidence=confidence,
        family=family,
        parsed_suffix=parsed_suffix,
        notes=notes,
    )
